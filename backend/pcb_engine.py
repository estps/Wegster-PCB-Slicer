
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry

from gerber_io import DrillHole, LayerType, PcbProject

__all__ = [
    "ToolSpec",
    "DepthStrategy",
    "SlicerConfig",
    "Polyline",
    "DrillHit",
    "ToolpathGroup",
    "ToolpathPlan",
    "BoardGeometry",
    "SlicerError",
    "build_board",
    "plan_toolpaths",
    "select_registration_holes",
    "pin_axis",
    "reflect_about_pin_axis",
]


class SlicerError(Exception):
    pass


@dataclass
class ToolSpec:

    name: str = "0.1mm 30deg V-bit"
    kind: str = "vbit"
    diameter: float = 0.1
    tip_diameter: float = 0.0
    angle: float = 30.0

    def radius_at_depth(self, depth: float) -> float:
        depth = abs(float(depth))
        if self.kind == "vbit":
            return self.tip_diameter / 2.0 + depth * math.tan(math.radians(self.angle / 2.0))
        return self.diameter / 2.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "diameter": self.diameter,
            "tip_diameter": self.tip_diameter,
            "angle": self.angle,
        }


@dataclass
class DepthStrategy:

    total_depth: float = 0.05
    stepdown: float = 0.05
    final_spring_pass: bool = False

    def passes(self) -> list[float]:
        total = abs(self.total_depth)
        if total <= 0:
            return [0.0]
        step = abs(self.stepdown)
        if step <= 0:
            return [-total]

        depths: list[float] = []
        travelled = 0.0
        while travelled < total - 1e-9:
            travelled = min(total, travelled + step)
            depths.append(-travelled)
        if self.final_spring_pass and depths:
            depths.append(depths[-1])
        return depths


@dataclass
class SlicerConfig:

    machine_x: float = 140.0
    machine_y: float = 90.0
    machine_z: float = 40.0
    origin_mode: str = "lower_left"
    margin: float = 2.0
    enforce_envelope: bool = True

    isolation_tool: ToolSpec = field(
        default_factory=lambda: ToolSpec("0.1mm 30deg V-bit", "vbit", 0.1, 0.0, 30.0)
    )
    rubout_tool: ToolSpec = field(
        default_factory=lambda: ToolSpec("0.8mm flat end mill", "flat", 0.8)
    )
    cutout_tool: ToolSpec = field(
        default_factory=lambda: ToolSpec("1.0mm flat end mill", "flat", 1.0)
    )

    isolation_enabled: bool = True
    isolation_depth: float = 0.05
    isolation_passes: int = 1
    isolation_stepover: float = 0.0
    isolation_clearance: float = 0.0

    rubout_enabled: bool = False
    rubout_depth: float = 0.05
    rubout_stepover: float = 0.0
    rubout_clearance: float = 0.25
    rubout_max_iterations: int = 400

    cutout_enabled: bool = True
    cutout_depth: float = 1.8
    cutout_stepdown: float = 0.5
    tab_count: int = 4
    tab_width: float = 1.5
    tab_enabled: bool = True

    drill_enabled: bool = True
    board_thickness: float = 1.6
    drill_depth_extra: float = 0.3
    peck_depth: float = 0.4
    peck_retract: float = 0.5
    drill_only_plated: bool = False

    mill_top: bool = True
    mill_bottom: bool = False
    mirror_bottom: bool = True

    silkscreen_enabled: bool = False
    silkscreen_mode: str = "cutout"
    silkscreen_depth: float = 0.05
    silkscreen_clear_pads: bool = True
    silkscreen_pad_clearance: float = 0.10
    silkscreen_tool: ToolSpec = field(
        default_factory=lambda: ToolSpec("0.3mm flat end mill", "flat", 0.3)
    )
    silkscreen_bottom: bool = True

    drill_first: bool = True
    registration_pins: int = 2

    flip_axis: str = "horizontal"

    alignment_holes: bool = False
    alignment_hole_depth: float = 10.0
    alignment_hole_diameter: float = 3.0
    alignment_pin1_x: float = 0.0
    alignment_pin1_y: float = 0.0
    alignment_pin2_x: float = 80.0
    alignment_pin2_y: float = 0.0

    split_output: bool = True

    quad_segs: int = 8
    simplify_tolerance: float = 0.002
    travel_optimise: bool = True

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.machine_x <= 0 or self.machine_y <= 0 or self.machine_z <= 0:
            problems.append("Machine envelope must be positive.")
        if self.origin_mode not in ("lower_left", "center"):
            problems.append(f"Unknown origin mode {self.origin_mode!r}.")
        if self.isolation_enabled and self.isolation_passes < 1:
            problems.append("Isolation passes must be at least 1.")
        if self.isolation_enabled and self.isolation_depth <= 0:
            problems.append("Isolation depth must be greater than zero.")
        if self.cutout_enabled and self.cutout_depth <= 0:
            problems.append("Cut-out depth must be greater than zero.")
        if self.cutout_enabled and self.cutout_tool.diameter <= 0:
            problems.append("Cut-out tool diameter must be greater than zero.")
        if self.tab_enabled and self.cutout_enabled and self.tab_width < 0:
            problems.append("Tab width cannot be negative.")
        if self.rubout_enabled and self.rubout_tool.diameter <= 0:
            problems.append("Rub-out tool diameter must be greater than zero.")
        if not self.mill_top and not self.mill_bottom:
            problems.append("At least one side must be selected.")
        if self.flip_axis not in ("horizontal", "vertical"):
            problems.append(f"Unknown flip axis {self.flip_axis!r}.")
        if self.silkscreen_enabled and self.silkscreen_depth <= 0:
            problems.append("Silkscreen depth must be greater than zero.")
        if self.silkscreen_enabled and self.silkscreen_mode not in ("cutout", "engrave"):
            problems.append(f"Unknown silkscreen mode {self.silkscreen_mode!r}.")
        if self.alignment_holes and self.alignment_hole_diameter <= 0:
            problems.append("Alignment hole diameter must be greater than zero.")
        if self.alignment_holes and self.alignment_hole_depth <= 0:
            problems.append("Alignment hole depth must be greater than zero.")
        return problems


    def pins(self) -> tuple[tuple[float, float], tuple[float, float]]:
        return (
            (self.alignment_pin1_x, self.alignment_pin1_y),
            (self.alignment_pin2_x, self.alignment_pin2_y),
        )


@dataclass
class Polyline:

    points: np.ndarray
    closed: bool = False
    kind: str = "cut"

    def __post_init__(self) -> None:
        self.points = np.asarray(self.points, dtype=float).reshape(-1, 2)

    @property
    def length(self) -> float:
        if len(self.points) < 2:
            return 0.0
        deltas = np.diff(self.points, axis=0)
        return float(np.sum(np.hypot(deltas[:, 0], deltas[:, 1])))

    @property
    def start(self) -> tuple[float, float]:
        return (float(self.points[0, 0]), float(self.points[0, 1]))

    @property
    def end(self) -> tuple[float, float]:
        return (float(self.points[-1, 0]), float(self.points[-1, 1]))

    def reversed_copy(self) -> "Polyline":
        return Polyline(self.points[::-1].copy(), self.closed, self.kind)

    def to_dict(self, precision: int = 4) -> dict:
        return {
            "points": np.round(self.points, precision).tolist(),
            "closed": self.closed,
            "kind": self.kind,
        }


@dataclass
class DrillHit:
    x: float
    y: float
    diameter: float
    plated: bool = True
    registration: bool = False
    depth: float | None = None


@dataclass
class ToolpathGroup:

    name: str
    kind: str
    tool: ToolSpec
    depth: DepthStrategy
    polylines: list[Polyline] = field(default_factory=list)
    holes: list[DrillHit] = field(default_factory=list)
    side: str = "top"
    requires_flip_before: bool = False
    skip_tool_change: bool = False

    @property
    def cut_length(self) -> float:
        return sum(p.length for p in self.polylines)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "side": self.side,
            "tool": self.tool.to_dict(),
            "depth": {
                "total_depth": self.depth.total_depth,
                "stepdown": self.depth.stepdown,
                "passes": self.depth.passes(),
            },
            "requires_flip_before": self.requires_flip_before,
            "cut_length": round(self.cut_length, 3),
            "polylines": [p.to_dict() for p in self.polylines],
            "holes": [
                {"x": h.x, "y": h.y, "diameter": h.diameter, "plated": h.plated}
                for h in self.holes
            ],
        }


@dataclass
class ToolpathPlan:

    board: "BoardGeometry"
    groups: list[ToolpathGroup] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_cut_length(self) -> float:
        return sum(g.cut_length * max(1, len(g.depth.passes())) for g in self.groups)

    def to_dict(self) -> dict:
        return {
            "board": self.board.to_dict(),
            "groups": [g.to_dict() for g in self.groups],
            "warnings": self.warnings,
            "total_cut_length": round(self.total_cut_length, 3),
        }


def iter_polygons(geom: BaseGeometry | None) -> Iterator[Polygon]:
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, Polygon):
        yield geom
    elif isinstance(geom, (MultiPolygon, GeometryCollection)):
        for part in geom.geoms:
            yield from iter_polygons(part)


def iter_rings(geom: BaseGeometry | None) -> Iterator[np.ndarray]:
    for polygon in iter_polygons(geom):
        if polygon.exterior is not None:
            yield np.asarray(polygon.exterior.coords, dtype=float)
        for interior in polygon.interiors:
            yield np.asarray(interior.coords, dtype=float)


def largest_polygon(geom: BaseGeometry | None) -> Polygon | None:
    best: Polygon | None = None
    best_area = -1.0
    for polygon in iter_polygons(geom):
        if polygon.area > best_area:
            best = polygon
            best_area = polygon.area
    return best


def pin_axis(
    p1: tuple[float, float], p2: tuple[float, float]
) -> tuple[str, float]:
    if abs(p1[1] - p2[1]) < 1e-6:
        return ("vertical", (p1[0] + p2[0]) / 2.0)
    if abs(p1[0] - p2[0]) < 1e-6:
        return ("horizontal", (p1[1] + p2[1]) / 2.0)
    return ("diagonal", 0.0)


def reflect_about_pin_axis(
    geom: BaseGeometry,
    p1: tuple[float, float],
    p2: tuple[float, float],
) -> BaseGeometry:
    mid_x = (p1[0] + p2[0]) / 2.0
    mid_y = (p1[1] + p2[1]) / 2.0
    delta_x = p2[0] - p1[0]
    delta_y = p2[1] - p1[1]
    if abs(delta_x) < 1e-9 and abs(delta_y) < 1e-9:
        return geom

    angle = math.degrees(math.atan2(delta_y, delta_x))
    result = affinity.translate(geom, xoff=-mid_x, yoff=-mid_y)
    result = affinity.rotate(result, -angle, origin=(0.0, 0.0))
    result = affinity.scale(result, xfact=-1.0, yfact=1.0, origin=(0.0, 0.0))
    result = affinity.rotate(result, angle, origin=(0.0, 0.0))
    return affinity.translate(result, xoff=mid_x, yoff=mid_y)


def _clean(geom: BaseGeometry | None) -> BaseGeometry | None:
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.is_empty:
        return None
    return geom


def _buffer(
    geom: BaseGeometry,
    distance: float,
    quad_segs: int,
    join_style: str = "round",
) -> BaseGeometry | None:
    if geom is None or geom.is_empty:
        return None
    result = geom.buffer(
        distance,
        quad_segs=max(2, quad_segs),
        join_style=join_style,
        cap_style="round",
    )
    return _clean(result)


def _ring_to_polyline(
    ring: np.ndarray, tolerance: float, kind: str
) -> Polyline | None:
    if len(ring) < 4:
        return None
    if tolerance > 0:
        simplified = LineString(ring).simplify(tolerance, preserve_topology=False)
        coords = np.asarray(simplified.coords, dtype=float)
        if len(coords) >= 3 and not np.allclose(coords[0], coords[-1]):
            coords = np.vstack([coords, coords[0]])
    else:
        coords = ring
    if len(coords) < 4:
        return None
    return Polyline(coords, closed=True, kind=kind)


def _cumulative_lengths(points: np.ndarray) -> np.ndarray:
    deltas = np.diff(points, axis=0)
    segment_lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    return np.concatenate([[0.0], np.cumsum(segment_lengths)])


def _point_at(points: np.ndarray, cumulative: np.ndarray, distance: float) -> tuple[float, float]:
    total = cumulative[-1]
    if total <= 0:
        return (float(points[0, 0]), float(points[0, 1]))
    distance = min(max(distance, 0.0), total)
    index = int(np.searchsorted(cumulative, distance, side="right") - 1)
    index = min(max(index, 0), len(points) - 2)
    span = cumulative[index + 1] - cumulative[index]
    if span <= 1e-12:
        return (float(points[index, 0]), float(points[index, 1]))
    t = (distance - cumulative[index]) / span
    x = points[index, 0] + t * (points[index + 1, 0] - points[index, 0])
    y = points[index, 1] + t * (points[index + 1, 1] - points[index, 1])
    return (float(x), float(y))


def _slice_ring(points: np.ndarray, cumulative: np.ndarray, start: float, end: float) -> np.ndarray:
    start = max(start, 0.0)
    end = min(end, cumulative[-1])
    if end - start < 1e-9:
        return np.empty((0, 2))

    collected: list[tuple[float, float]] = [_point_at(points, cumulative, start)]
    inside = (cumulative > start) & (cumulative < end)
    collected.extend((float(p[0]), float(p[1])) for p in points[inside])
    collected.append(_point_at(points, cumulative, end))
    return np.asarray(collected, dtype=float)


def _optimise_order(
    polylines: Sequence[Polyline], start: tuple[float, float]
) -> list[Polyline]:
    remaining = list(polylines)
    ordered: list[Polyline] = []
    cursor = np.asarray(start, dtype=float)

    while remaining:
        best_index = 0
        best_distance = float("inf")
        best_flip = False
        for index, polyline in enumerate(remaining):
            head = polyline.points[0]
            tail = polyline.points[-1]
            forward = float(np.hypot(*(head - cursor)))
            if forward < best_distance:
                best_distance = forward
                best_index = index
                best_flip = False
            if not polyline.closed:
                backward = float(np.hypot(*(tail - cursor)))
                if backward < best_distance:
                    best_distance = backward
                    best_index = index
                    best_flip = True
        chosen = remaining.pop(best_index)
        if best_flip:
            chosen = chosen.reversed_copy()
        ordered.append(chosen)
        cursor = chosen.points[-1]

    return ordered


@dataclass
class BoardGeometry:

    outline: Polygon
    copper: dict[str, BaseGeometry]
    holes: list[DrillHit]
    offset: tuple[float, float]
    source_size: tuple[float, float]
    silkscreen: dict[str, BaseGeometry] = field(default_factory=dict)
    pads: dict[str, BaseGeometry] = field(default_factory=dict)

    @property
    def width(self) -> float:
        return float(self.outline.bounds[2] - self.outline.bounds[0])

    @property
    def height(self) -> float:
        return float(self.outline.bounds[3] - self.outline.bounds[1])

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return tuple(float(v) for v in self.outline.bounds)

    def copper_preview(self, point_budget: int = 40_000) -> dict[str, list[list[list[float]]]]:
        preview: dict[str, list[list[list[float]]]] = {}
        for side, geom in self.copper.items():
            rings: list[list[list[float]]] = []
            tolerance = 0.02
            for _ in range(6):
                simplified = shapely.simplify(geom, tolerance, preserve_topology=True)
                rings = []
                total = 0
                for ring in iter_rings(simplified):
                    if len(ring) < 4:
                        continue
                    rings.append([[round(float(x), 4), round(float(y), 4)] for x, y in ring])
                    total += len(ring)
                    if total > point_budget:
                        break
                if total <= point_budget:
                    break
                tolerance *= 3.0
            preview[side] = rings
        return preview

    def to_dict(self, include_copper: bool = True) -> dict:
        minx, miny, maxx, maxy = self.outline.bounds
        payload = {
            "width": round(self.width, 3),
            "height": round(self.height, 3),
            "offset": [round(self.offset[0], 4), round(self.offset[1], 4)],
            "source_size": [
                round(self.source_size[0], 3),
                round(self.source_size[1], 3),
            ],
            "bounds": [round(v, 3) for v in (minx, miny, maxx, maxy)],
            "outline": [
                [round(float(x), 4), round(float(y), 4)] for x, y in self.outline.exterior.coords
            ],
        }
        if include_copper:
            payload["copper_preview"] = self.copper_preview()
        return payload


def _outline_from_layers(project: PcbProject) -> tuple[Polygon, float]:
    outline_layer = project.outline
    if outline_layer is None or outline_layer.geometry is None:
        raise SlicerError("The project has no usable board outline (Edge.Cuts) layer.")

    shell = largest_polygon(_clean(outline_layer.geometry))
    if shell is None or shell.is_empty or shell.area <= 0:
        raise SlicerError("Board outline layer contains no closed contour.")

    exterior_poly = Polygon(shell.exterior)
    perimeter = exterior_poly.exterior.length
    stroke = 0.0
    if perimeter > 1e-6:
        stroke = shell.area / perimeter
    stroke = max(0.0, min(stroke, 2.0))

    board = _clean(exterior_poly.buffer(-stroke / 2.0)) or exterior_poly

    cutters: list[BaseGeometry] = []
    for polygon in iter_polygons(_clean(outline_layer.geometry)):
        if polygon is shell:
            continue
        if polygon.area <= 0:
            continue
        cutter = Polygon(polygon.exterior)
        if stroke > 0:
            cutter = _clean(cutter.buffer(-stroke / 2.0)) or cutter
        cutters.append(cutter)
    if cutters:
        board = _clean(shapely.difference(board, shapely.union_all(cutters))) or board

    result = largest_polygon(board)
    if result is None:
        raise SlicerError("Failed to derive a usable board outline polygon.")
    return result, stroke


def _outline_from_features(project: PcbProject, margin: float) -> Polygon:
    parts: list[BaseGeometry] = []
    for layer in project.copper_layers:
        if layer.geometry is not None and not layer.geometry.is_empty:
            parts.append(layer.geometry)
    for hole in project.all_holes:
        parts.append(Point(hole.x, hole.y).buffer(max(hole.diameter, 0.5) / 2.0))
    if not parts:
        raise SlicerError("Cannot infer a board outline: no copper or drill data.")
    hull = shapely.union_all(parts).convex_hull
    return hull.buffer(margin, join_style="mitre", quad_segs=1)


def build_board(project: PcbProject, config: SlicerConfig) -> BoardGeometry:
    warnings: list[str] = []
    try:
        outline, _stroke = _outline_from_layers(project)
    except SlicerError as exc:
        warnings.append(f"{exc} Falling back to copper extents.")
        outline = _outline_from_features(project, 1.0)

    minx, miny, maxx, maxy = outline.bounds
    source_width = maxx - minx
    source_height = maxy - miny
    if source_width <= 0 or source_height <= 0:
        raise SlicerError("Board outline has zero extent.")

    margin_x = margin_y = config.margin
    if config.origin_mode == "center":
        offset_x = max(margin_x, (config.machine_x - source_width) / 2.0)
        offset_y = max(margin_y, (config.machine_y - source_height) / 2.0)
    else:
        offset_x = margin_x
        offset_y = margin_y

    if config.alignment_holes:
        axis_kind, axis_coordinate = pin_axis(*config.pins())
        if axis_kind == "vertical":
            offset_x = axis_coordinate - source_width / 2.0
        elif axis_kind == "horizontal":
            offset_y = axis_coordinate - source_height / 2.0

    shift_x = offset_x - minx
    shift_y = offset_y - miny

    def to_machine(geom: BaseGeometry | None) -> BaseGeometry | None:
        if geom is None or geom.is_empty:
            return None
        return affinity.translate(geom, xoff=shift_x, yoff=shift_y)

    machine_outline = to_machine(outline)
    assert machine_outline is not None

    def flip(geom: BaseGeometry, side: str) -> BaseGeometry:
        if side != "bottom" or not config.mirror_bottom:
            return geom
        if config.alignment_holes:
            return reflect_about_pin_axis(geom, *config.pins())
        if config.flip_axis == "vertical":
            axis = offset_x + source_width / 2.0
            return affinity.scale(geom, xfact=-1.0, yfact=1.0, origin=(axis, 0.0))
        axis = offset_y + source_height / 2.0
        return affinity.scale(geom, xfact=1.0, yfact=-1.0, origin=(0.0, axis))

    copper: dict[str, BaseGeometry] = {}
    silkscreen: dict[str, BaseGeometry] = {}
    pads: dict[str, BaseGeometry] = {}

    for side, layer_type, silk_type, mask_type in (
        ("top", LayerType.TOP_COPPER, LayerType.TOP_SILK, LayerType.TOP_MASK),
        (
            "bottom",
            LayerType.BOTTOM_COPPER,
            LayerType.BOTTOM_SILK,
            LayerType.BOTTOM_MASK,
        ),
    ):
        for store, source in ((copper, layer_type), (silkscreen, silk_type), (pads, mask_type)):
            layer = project.layer(source)
            if layer is None or layer.geometry is None:
                continue
            geom = _clean(layer.geometry)
            if geom is None:
                continue
            geom = to_machine(geom)
            if geom is None:
                continue
            store[side] = flip(geom, side)

    holes = [
        DrillHit(
            x=hole.x + shift_x,
            y=hole.y + shift_y,
            diameter=hole.diameter,
            plated=hole.plated,
        )
        for hole in project.all_holes
    ]

    return BoardGeometry(
        outline=machine_outline,
        copper=copper,
        holes=holes,
        offset=(offset_x, offset_y),
        source_size=(source_width, source_height),
        silkscreen=silkscreen,
        pads=pads,
    )


def generate_isolation(
    copper: BaseGeometry,
    board: Polygon,
    tool: ToolSpec,
    config: SlicerConfig,
    depth: float | None = None,
) -> list[Polyline]:
    depth = config.isolation_depth if depth is None else depth
    radius = tool.radius_at_depth(depth)
    if radius <= 0:
        raise SlicerError("Isolation tool has zero cutting radius.")

    cut_width = 2.0 * radius
    stepover = config.isolation_stepover or 0.8 * cut_width
    stepover = max(stepover, 1e-3)

    reach = radius + config.isolation_clearance + stepover * max(config.isolation_passes - 1, 0)
    clip = _buffer(board, reach + cut_width, config.quad_segs, join_style="mitre")

    polylines: list[Polyline] = []
    seen: set[tuple[int, int]] = set()

    for index in range(config.isolation_passes):
        offset = radius + config.isolation_clearance + index * stepover
        buffered = _buffer(copper, offset, config.quad_segs)
        if buffered is None:
            continue
        if clip is not None:
            buffered = _clean(shapely.intersection(buffered, clip))
            if buffered is None:
                continue
        for ring in iter_rings(buffered):
            polyline = _ring_to_polyline(ring, config.simplify_tolerance, "isolation")
            if polyline is None:
                continue
            key = (
                int(round(polyline.points[0, 0] * 1000)),
                int(round(polyline.points[0, 1] * 1000)),
            )
            if len(polyline.points) < 4 or polyline.length < 4 * config.simplify_tolerance:
                continue
            if key in seen:
                continue
            seen.add(key)
            polylines.append(polyline)

    return polylines


def generate_alignment_holes(
    board: BoardGeometry, config: SlicerConfig
) -> list[DrillHit]:
    del board
    if not config.alignment_holes:
        return []

    holes: list[DrillHit] = []
    for x, y in config.pins():
        if x < 0 or y < 0 or x > config.machine_x or y > config.machine_y:
            raise SlicerError(
                f"Alignment pin at ({x:.1f}, {y:.1f}) mm falls outside the "
                f"{config.machine_x:.0f} x {config.machine_y:.0f} mm work area. "
                "Move the pin or turn alignment holes off."
            )
        holes.append(
            DrillHit(
                x=x,
                y=y,
                diameter=config.alignment_hole_diameter,
                plated=False,
                registration=True,
                depth=config.alignment_hole_depth,
            )
        )
    return holes


def generate_silkscreen_cutout(
    openings: BaseGeometry | None,
    tool: ToolSpec,
    config: SlicerConfig,
) -> list[Polyline]:
    radius = tool.radius_at_depth(config.silkscreen_depth)
    if radius <= 0:
        raise SlicerError("Silkscreen tool has zero cutting radius.")

    region = _clean(openings)
    if region is None:
        return []

    polylines: list[Polyline] = []
    stepover = max(0.7 * 2.0 * radius, 0.05)

    for polygon in iter_polygons(region):
        perimeter = polygon.exterior.length if polygon.exterior else 0.0
        inscribed = (2.0 * polygon.area / perimeter) if perimeter > 0 else 0.0
        if inscribed <= radius:
            centre = polygon.representative_point()
            polylines.append(
                Polyline(
                    np.asarray([[centre.x, centre.y], [centre.x, centre.y]]),
                    closed=False,
                    kind="silkscreen",
                )
            )
            continue

        current: BaseGeometry | None = polygon
        for _ in range(config.rubout_max_iterations):
            if current is None or current.is_empty:
                break
            for ring in iter_rings(current):
                polyline = _ring_to_polyline(
                    ring, config.simplify_tolerance, "silkscreen"
                )
                if polyline is not None and polyline.length > 0.05:
                    polylines.append(polyline)
            current = _buffer(current, -stepover, config.quad_segs)

    return polylines


def generate_silkscreen(
    silk: BaseGeometry,
    pads: BaseGeometry | None,
    board: Polygon,
    config: SlicerConfig,
) -> list[Polyline]:
    tool = config.silkscreen_tool
    radius = tool.radius_at_depth(config.silkscreen_depth)
    if radius <= 0:
        raise SlicerError("Silkscreen tool has zero cutting radius.")

    geom = _clean(silk)
    if geom is None:
        return []

    if config.silkscreen_clear_pads and pads is not None and not pads.is_empty:
        keepout = _buffer(pads, config.silkscreen_pad_clearance, config.quad_segs)
        if keepout is not None:
            geom = _clean(shapely.difference(geom, keepout))
            if geom is None:
                return []

    geom = _buffer(geom, radius, config.quad_segs)
    if geom is None:
        return []

    polylines: list[Polyline] = []
    for ring in iter_rings(geom):
        polyline = _ring_to_polyline(ring, config.simplify_tolerance, "silkscreen")
        if polyline is not None and polyline.length > 0.15:
            polylines.append(polyline)
    return polylines


def generate_rubout(
    copper: BaseGeometry,
    board: Polygon,
    tool: ToolSpec,
    config: SlicerConfig,
) -> list[Polyline]:
    radius = tool.radius_at_depth(config.rubout_depth)
    if radius <= 0:
        raise SlicerError("Rub-out tool has zero cutting radius.")

    keepout = _buffer(
        copper, radius + config.rubout_clearance, config.quad_segs
    )
    region = board
    if keepout is not None:
        region = _clean(shapely.difference(board, keepout))
    if region is None:
        return []

    stepover = config.rubout_stepover or 1.4 * radius
    stepover = max(stepover, 0.05)

    polylines: list[Polyline] = []
    current: BaseGeometry | None = region
    iteration = 0

    while current is not None and not current.is_empty and iteration < config.rubout_max_iterations:
        iteration += 1
        for ring in iter_rings(current):
            polyline = _ring_to_polyline(ring, config.simplify_tolerance, "rubout")
            if polyline is not None and polyline.length > 0.2:
                polylines.append(polyline)
        shrunk = _buffer(current, -stepover, config.quad_segs)
        if shrunk is None:
            break
        current = shrunk

    return polylines


def _choose_tab_positions(
    ring: np.ndarray, tab_count: int, tab_width: float
) -> list[float]:
    cumulative = _cumulative_lengths(ring)
    total = float(cumulative[-1])
    if total <= 0 or tab_count <= 0:
        return []

    positions: list[float] = []
    for index in range(tab_count):
        target = total * (index + 0.5) / tab_count
        best = target
        best_score = float("inf")
        samples = 41
        for step in range(samples):
            candidate = target + (step - samples // 2) * (tab_width / (samples // 2))
            candidate = min(max(candidate, 0.0), total)
            before = _point_at(ring, cumulative, candidate - tab_width / 2.0)
            middle = _point_at(ring, cumulative, candidate)
            after = _point_at(ring, cumulative, candidate + tab_width / 2.0)
            v1 = (middle[0] - before[0], middle[1] - before[1])
            v2 = (after[0] - middle[0], after[1] - middle[1])
            n1 = math.hypot(*v1)
            n2 = math.hypot(*v2)
            if n1 < 1e-9 or n2 < 1e-9:
                continue
            cosine = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
            score = math.acos(cosine)
            if score < best_score:
                best_score = score
                best = candidate
        positions.append(best)
    return sorted(positions)


def _apply_tabs(ring: np.ndarray, positions: Sequence[float], tab_width: float) -> list[np.ndarray]:
    cumulative = _cumulative_lengths(ring)
    total = float(cumulative[-1])
    if total <= 0:
        return []

    cuts: list[tuple[float, float]] = []
    for position in positions:
        start = position - tab_width / 2.0
        end = position + tab_width / 2.0
        if start < 0.0:
            cuts.append((0.0, max(end, 0.0)))
            cuts.append((total + start, total))
        elif end > total:
            cuts.append((start, total))
            cuts.append((0.0, end - total))
        else:
            cuts.append((start, end))

    cuts.sort()
    merged: list[list[float]] = []
    for start, end in cuts:
        if merged and start <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start - cursor > 1e-6:
            keeps.append((cursor, start))
        cursor = max(cursor, end)
    if total - cursor > 1e-6:
        keeps.append((cursor, total))

    if not keeps:
        return []

    if len(keeps) > 1 and keeps[0][0] <= 1e-6 and keeps[-1][1] >= total - 1e-6:
        first = keeps.pop(0)
        last = keeps.pop()
        head = _slice_ring(ring, cumulative, last[0], last[1])
        tail = _slice_ring(ring, cumulative, first[0], first[1])
        stitched = np.vstack([head, tail[1:]]) if len(tail) > 1 else head
        return [stitched] + [
            _slice_ring(ring, cumulative, start, end) for start, end in keeps
        ]

    return [_slice_ring(ring, cumulative, start, end) for start, end in keeps]


def generate_edge_cut(
    board: Polygon,
    tool: ToolSpec,
    config: SlicerConfig,
) -> list[Polyline]:
    radius = tool.radius_at_depth(config.cutout_depth)
    if radius <= 0:
        raise SlicerError("Cut-out tool has zero cutting radius.")

    centerline = _buffer(board, -radius, config.quad_segs)
    if centerline is None:
        raise SlicerError(
            "Board is too small for the selected cut-out tool "
            f"(needs at least {2 * radius:.2f} mm in every direction)."
        )

    polylines: list[Polyline] = []
    rings = list(iter_rings(centerline))
    if not rings:
        return []

    rings.sort(key=lambda r: -abs(_signed_area(r)))

    for index, ring in enumerate(rings):
        is_outer = index == 0
        if config.tab_enabled and config.tab_count > 0 and is_outer and config.tab_width > 0:
            positions = _choose_tab_positions(ring, config.tab_count, config.tab_width)
            pieces = _apply_tabs(ring, positions, config.tab_width)
            for piece in pieces:
                if len(piece) >= 2:
                    polyline = Polyline(piece, closed=False, kind="cutout")
                    if polyline.length > 0.05:
                        polylines.append(polyline)
            continue

        polyline = _ring_to_polyline(ring, config.simplify_tolerance, "cutout")
        if polyline is not None:
            polylines.append(polyline)

    return polylines


def _signed_area(ring: np.ndarray) -> float:
    if len(ring) < 3:
        return 0.0
    x = ring[:, 0]
    y = ring[:, 1]
    return float(0.5 * np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def generate_drilling(holes: Sequence[DrillHit]) -> dict[float, list[DrillHit]]:
    grouped: dict[float, list[DrillHit]] = {}
    for hole in holes:
        if hole.diameter <= 0:
            continue
        grouped.setdefault(round(hole.diameter, 3), []).append(hole)
    return dict(sorted(grouped.items()))


def _drill_depth(config: SlicerConfig, board: BoardGeometry, diameter: float) -> float:
    del board, diameter
    return config.board_thickness + config.drill_depth_extra


def select_registration_holes(
    board: BoardGeometry, count: int = 2
) -> list[DrillHit]:
    count = max(0, min(int(count), 4))
    usable = [h for h in board.holes if h.diameter >= 0.8 and not h.registration]
    if len(usable) < count:
        usable = sorted(board.holes, key=lambda h: -h.diameter)
    if len(usable) < count:
        return []

    chosen = [max(usable, key=lambda h: h.diameter)]
    remaining = [h for h in usable if h is not chosen[0]]
    while len(chosen) < count and remaining:
        best = max(
            remaining,
            key=lambda h: min(
                math.hypot(h.x - c.x, h.y - c.y) for c in chosen
            ),
        )
        chosen.append(best)
        remaining = [h for h in remaining if h is not best]
    return chosen


def plan_toolpaths(project: PcbProject, config: SlicerConfig) -> ToolpathPlan:
    problems = config.validate()
    if problems:
        raise SlicerError("; ".join(problems))

    board = build_board(project, config)
    plan = ToolpathPlan(board=board)

    if config.alignment_holes and config.mill_bottom:
        reflected = reflect_about_pin_axis(board.outline, *config.pins())
        original = board.outline.bounds
        flipped = reflected.bounds
        shift = max(abs(original[i] - flipped[i]) for i in range(4)) / 2.0
        if shift > 0.25:
            plan.warnings.append(
                f"The board is not centred on the pin line, so the flipped side "
                f"would be offset by about {shift:.1f} mm. Move the board or the "
                f"pins so they are symmetric about it."
            )

    if config.enforce_envelope:
        width = board.width
        height = board.height
        if width > config.machine_x or height > config.machine_y:
            raise SlicerError(
                f"Board is {width:.1f} x {height:.1f} mm which does not fit the "
                f"{config.machine_x:.0f} x {config.machine_y:.0f} mm work area."
            )
        max_x = board.outline.bounds[2]
        max_y = board.outline.bounds[3]
        if max_x > config.machine_x or max_y > config.machine_y:
            raise SlicerError(
                f"Board at origin {config.origin_mode!r} reaches ({max_x:.1f}, "
                f"{max_y:.1f}) mm which is outside the work area."
            )

    alignment = generate_alignment_holes(board, config)

    sides: list[str] = []
    if config.mill_top:
        sides.append("top")
    if config.mill_bottom:
        sides.append("bottom")

    flip_pending = False
    for side in sides:
        copper = board.copper.get(side)
        if copper is None:
            plan.warnings.append(f"No copper geometry for the {side} side; skipped.")
            continue

        if config.isolation_enabled:
            paths = generate_isolation(copper, board.outline, config.isolation_tool, config)
            if paths:
                plan.groups.append(
                    ToolpathGroup(
                        name=f"Isolation ({side})",
                        kind="isolation",
                        tool=config.isolation_tool,
                        depth=DepthStrategy(config.isolation_depth, config.isolation_depth),
                        polylines=paths,
                        side=side,
                        requires_flip_before=flip_pending,
                    )
                )
                flip_pending = False
            else:
                plan.warnings.append(f"Isolation produced no toolpaths on the {side} side.")

        if config.rubout_enabled:
            paths = generate_rubout(copper, board.outline, config.rubout_tool, config)
            if paths:
                plan.groups.append(
                    ToolpathGroup(
                        name=f"Rub-out ({side})",
                        kind="rubout",
                        tool=config.rubout_tool,
                        depth=DepthStrategy(config.rubout_depth, config.rubout_depth),
                        polylines=paths,
                        side=side,
                        requires_flip_before=flip_pending,
                    )
                )
                flip_pending = False

        if side == "top" and config.mill_bottom:
            flip_pending = True

    if config.silkscreen_enabled:
        silk_sides: list[str] = []
        if config.mill_top:
            silk_sides.append("top")
        if config.mill_bottom and config.silkscreen_bottom:
            silk_sides.append("bottom")

        for side in silk_sides:
            if config.silkscreen_mode == "cutout":
                openings = board.pads.get(side)
                if openings is None or openings.is_empty:
                    plan.warnings.append(
                        f"No pad openings found on the {side} side, so there is "
                        f"nothing to cut the silkscreen away from; skipped."
                    )
                    continue
                paths = generate_silkscreen_cutout(
                    openings, config.silkscreen_tool, config
                )
                label = f"Silkscreen cut-out ({side})"
            else:
                silk = board.silkscreen.get(side)
                if silk is None or silk.is_empty:
                    plan.warnings.append(
                        f"No silkscreen artwork found for the {side} side; skipped."
                    )
                    continue
                paths = generate_silkscreen(
                    silk, board.pads.get(side), board.outline, config
                )
                label = f"Silkscreen engrave ({side})"

            if not paths:
                plan.warnings.append(
                    f"Silkscreen produced no toolpaths on the {side} side."
                )
                continue
            plan.groups.append(
                ToolpathGroup(
                    name=label,
                    kind="silkscreen",
                    tool=config.silkscreen_tool,
                    depth=DepthStrategy(
                        config.silkscreen_depth, config.silkscreen_depth
                    ),
                    polylines=paths,
                    side=side,
                )
            )

    grouped: dict[float, list[DrillHit]] = {}
    if config.drill_enabled:
        holes = [
            h for h in board.holes if not (config.drill_only_plated and not h.plated)
        ]
        grouped = generate_drilling(holes)

    for diameter, group_holes in grouped.items():
        plan.groups.append(
            ToolpathGroup(
                name=f"Drill {diameter:.3f} mm ({len(group_holes)} holes)",
                kind="drill",
                tool=ToolSpec(
                    name=f"{diameter:.3f} mm drill", kind="drill", diameter=diameter
                ),
                depth=DepthStrategy(
                    _drill_depth(config, board, diameter), config.peck_depth
                ),
                polylines=[],
                holes=list(group_holes),
                side="top",
            )
        )

    if config.drill_enabled and not grouped:
        plan.warnings.append("No valid drill hits found.")

    if alignment:
        plan.groups.insert(
            0,
            ToolpathGroup(
                name=f"Alignment pins ({len(alignment)} holes, "
                f"{config.alignment_hole_depth:.0f} mm deep)",
                kind="alignment",
                tool=ToolSpec(
                    f"{config.alignment_hole_diameter:.2f} mm pin drill",
                    "drill",
                    config.alignment_hole_diameter,
                ),
                depth=DepthStrategy(config.alignment_hole_depth, config.peck_depth),
                holes=alignment,
                side="top",
                skip_tool_change=True,
            ),
        )

    if config.cutout_enabled:
        paths = generate_edge_cut(board.outline, config.cutout_tool, config)
        if paths:
            plan.groups.append(
                ToolpathGroup(
                    name="Board cut-out",
                    kind="cutout",
                    tool=config.cutout_tool,
                    depth=DepthStrategy(config.cutout_depth, config.cutout_stepdown),
                    polylines=paths,
                    side="top",
                )
            )
        else:
            plan.warnings.append("Cut-out produced no toolpaths.")

    if config.travel_optimise:
        cursor = (0.0, 0.0)
        for group in plan.groups:
            if group.polylines:
                group.polylines = _optimise_order(group.polylines, cursor)
                cursor = group.polylines[-1].points[-1]
            if group.holes:
                group.holes = _optimise_holes(group.holes, cursor)
                if group.holes:
                    last = group.holes[-1]
                    cursor = np.asarray([last.x, last.y], dtype=float)

    return plan


def _optimise_holes(holes: Sequence[DrillHit], start: tuple[float, float]) -> list[DrillHit]:
    remaining = list(holes)
    ordered: list[DrillHit] = []
    cursor = np.asarray(start, dtype=float)
    while remaining:
        best_index = 0
        best_distance = float("inf")
        for index, hole in enumerate(remaining):
            distance = float(np.hypot(hole.x - cursor[0], hole.y - cursor[1]))
            if distance < best_distance:
                best_distance = distance
                best_index = index
        chosen = remaining.pop(best_index)
        ordered.append(chosen)
        cursor = np.asarray([chosen.x, chosen.y], dtype=float)
    return ordered
