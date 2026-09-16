
from __future__ import annotations

import ast
import math
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

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

__all__ = [
    "LayerType",
    "LayerData",
    "DrillHole",
    "DrillData",
    "PcbProject",
    "GerberError",
    "GerberParseError",
    "ExcellonParseError",
    "GerberParser",
    "ExcellonParser",
    "detect_layers",
    "load_project",
]


INCH_TO_MM = 25.4

GRID_MM = 1e-6

ARC_TOLERANCE_MM = 0.002


class GerberError(Exception):
    pass


class GerberParseError(GerberError):
    pass


class ExcellonParseError(GerberError):
    pass


class LayerType(str, Enum):
    TOP_COPPER = "top_copper"
    BOTTOM_COPPER = "bottom_copper"
    EDGE_CUTS = "edge_cuts"
    DRILL_PTH = "drill_pth"
    DRILL_NPTH = "drill_npth"
    TOP_SILK = "top_silk"
    BOTTOM_SILK = "bottom_silk"
    TOP_MASK = "top_mask"
    BOTTOM_MASK = "bottom_mask"
    TOP_PASTE = "top_paste"
    BOTTOM_PASTE = "bottom_paste"
    DOCUMENTATION = "documentation"
    UNKNOWN = "unknown"

    @property
    def is_copper(self) -> bool:
        return self in (LayerType.TOP_COPPER, LayerType.BOTTOM_COPPER)

    @property
    def is_drill(self) -> bool:
        return self in (LayerType.DRILL_PTH, LayerType.DRILL_NPTH)

    @property
    def display_name(self) -> str:
        return {
            LayerType.TOP_COPPER: "Front Copper",
            LayerType.BOTTOM_COPPER: "Back Copper",
            LayerType.EDGE_CUTS: "Board Outline",
            LayerType.DRILL_PTH: "Drill (Plated)",
            LayerType.DRILL_NPTH: "Drill (Non-Plated)",
            LayerType.TOP_SILK: "Front Silkscreen",
            LayerType.BOTTOM_SILK: "Back Silkscreen",
            LayerType.TOP_MASK: "Front Solder Mask",
            LayerType.BOTTOM_MASK: "Back Solder Mask",
            LayerType.TOP_PASTE: "Front Paste",
            LayerType.BOTTOM_PASTE: "Back Paste",
            LayerType.DOCUMENTATION: "Documentation",
            LayerType.UNKNOWN: "Unknown",
        }[self]


_EXTENSION_MAP: dict[str, tuple[LayerType, float]] = {
    ".gtl": (LayerType.TOP_COPPER, 0.99),
    ".gbl": (LayerType.BOTTOM_COPPER, 0.99),
    ".g1": (LayerType.TOP_COPPER, 0.9),
    ".g2": (LayerType.BOTTOM_COPPER, 0.9),
    ".gko": (LayerType.EDGE_CUTS, 0.99),
    ".gm1": (LayerType.EDGE_CUTS, 0.95),
    ".gml": (LayerType.EDGE_CUTS, 0.95),
    ".gto": (LayerType.TOP_SILK, 0.99),
    ".gbo": (LayerType.BOTTOM_SILK, 0.99),
    ".gts": (LayerType.TOP_MASK, 0.99),
    ".gbs": (LayerType.BOTTOM_MASK, 0.99),
    ".gtp": (LayerType.TOP_PASTE, 0.99),
    ".gbp": (LayerType.BOTTOM_PASTE, 0.99),
    ".gd1": (LayerType.DOCUMENTATION, 0.9),
    ".gdl": (LayerType.DOCUMENTATION, 0.9),
    ".drl": (LayerType.DRILL_PTH, 0.9),
    ".xln": (LayerType.DRILL_PTH, 0.9),
    ".nc": (LayerType.DRILL_PTH, 0.8),
    ".txt": (LayerType.DRILL_PTH, 0.35),
    ".gbr": (LayerType.UNKNOWN, 0.0),
    ".ger": (LayerType.UNKNOWN, 0.0),
    ".art": (LayerType.UNKNOWN, 0.0),
}

_NAME_SUFFIX_MAP: tuple[tuple[str, LayerType], ...] = (
    ("-f_cu", LayerType.TOP_COPPER),
    ("-b_cu", LayerType.BOTTOM_COPPER),
    ("-in1_cu", LayerType.TOP_COPPER),
    ("-in2_cu", LayerType.BOTTOM_COPPER),
    ("-edge_cuts", LayerType.EDGE_CUTS),
    ("-f_silks", LayerType.TOP_SILK),
    ("-b_silks", LayerType.BOTTOM_SILK),
    ("-f_mask", LayerType.TOP_MASK),
    ("-b_mask", LayerType.BOTTOM_MASK),
    ("-f_paste", LayerType.TOP_PASTE),
    ("-b_paste", LayerType.BOTTOM_PASTE),
    ("-f_fab", LayerType.DOCUMENTATION),
    ("-b_fab", LayerType.DOCUMENTATION),
    ("-f_courtyard", LayerType.DOCUMENTATION),
    ("-b_courtyard", LayerType.DOCUMENTATION),
)

_GERBER_EXTENSIONS = frozenset(
    {
        ".gbr",
        ".ger",
        ".art",
        ".gtl",
        ".gbl",
        ".gko",
        ".gm1",
        ".gml",
        ".gto",
        ".gbo",
        ".gts",
        ".gbs",
        ".gtp",
        ".gbp",
        ".gd1",
        ".gdl",
        ".g1",
        ".g2",
    }
)

_NAME_KEYWORDS: tuple[tuple[str, LayerType], ...] = (
    ("edge_cut", LayerType.EDGE_CUTS),
    ("edgecut", LayerType.EDGE_CUTS),
    ("boardoutline", LayerType.EDGE_CUTS),
    ("board_outline", LayerType.EDGE_CUTS),
    ("outline", LayerType.EDGE_CUTS),
    ("npth", LayerType.DRILL_NPTH),
    ("nonplated", LayerType.DRILL_NPTH),
    ("non_plated", LayerType.DRILL_NPTH),
    ("pth", LayerType.DRILL_PTH),
    ("plated", LayerType.DRILL_PTH),
    ("via", LayerType.DRILL_PTH),
    ("drill", LayerType.DRILL_PTH),
    ("topcopper", LayerType.TOP_COPPER),
    ("top_copper", LayerType.TOP_COPPER),
    ("toplayer", LayerType.TOP_COPPER),
    ("frontcopper", LayerType.TOP_COPPER),
    ("bottomcopper", LayerType.BOTTOM_COPPER),
    ("bottom_copper", LayerType.BOTTOM_COPPER),
    ("bottomlayer", LayerType.BOTTOM_COPPER),
    ("backcopper", LayerType.BOTTOM_COPPER),
    ("topsilk", LayerType.TOP_SILK),
    ("top_silk", LayerType.TOP_SILK),
    ("bottomsilk", LayerType.BOTTOM_SILK),
    ("bottom_silk", LayerType.BOTTOM_SILK),
    ("silkscreen", LayerType.TOP_SILK),
    ("topsolder", LayerType.TOP_MASK),
    ("top_solder", LayerType.TOP_MASK),
    ("topmask", LayerType.TOP_MASK),
    ("bottomsolder", LayerType.BOTTOM_MASK),
    ("bottom_solder", LayerType.BOTTOM_MASK),
    ("bottommask", LayerType.BOTTOM_MASK),
    ("toppaste", LayerType.TOP_PASTE),
    ("top_paste", LayerType.TOP_PASTE),
    ("bottompaste", LayerType.BOTTOM_PASTE),
    ("bottom_paste", LayerType.BOTTOM_PASTE),
    ("document", LayerType.DOCUMENTATION),
)


@dataclass
class LayerData:

    path: Path
    layer_type: LayerType
    confidence: float
    reason: str
    geometry: BaseGeometry | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def bounds(self) -> tuple[float, float, float, float] | None:
        if self.geometry is None or self.geometry.is_empty:
            return None
        minx, miny, maxx, maxy = self.geometry.bounds
        return (minx, miny, maxx, maxy)


@dataclass
class DrillHole:

    x: float
    y: float
    diameter: float
    plated: bool = True
    tool: int = 0

    @property
    def point(self) -> Point:
        return Point(self.x, self.y)


@dataclass
class DrillData:
    path: Path
    layer_type: LayerType
    holes: list[DrillHole] = field(default_factory=list)
    tools: dict[int, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    def by_diameter(self) -> dict[float, list[DrillHole]]:
        grouped: dict[float, list[DrillHole]] = {}
        for hole in self.holes:
            key = round(hole.diameter, 4)
            grouped.setdefault(key, []).append(hole)
        return dict(sorted(grouped.items()))


@dataclass
class PcbProject:

    source: Path
    layers: list[LayerData] = field(default_factory=list)
    drills: list[DrillData] = field(default_factory=list)
    work_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def layer(self, layer_type: LayerType) -> LayerData | None:
        for layer in self.layers:
            if layer.layer_type is layer_type:
                return layer
        return None

    def all_of(self, layer_type: LayerType) -> list[LayerData]:
        return [l for l in self.layers if l.layer_type is layer_type]

    @property
    def outline(self) -> LayerData | None:
        return self.layer(LayerType.EDGE_CUTS)

    @property
    def copper_layers(self) -> list[LayerData]:
        return [l for l in self.layers if l.layer_type.is_copper]

    @property
    def all_holes(self) -> list[DrillHole]:
        holes: list[DrillHole] = []
        for drill in self.drills:
            holes.extend(drill.holes)
        return holes

    def cleanup(self) -> None:
        if self.work_dir is not None and self.work_dir.exists():
            shutil.rmtree(self.work_dir, ignore_errors=True)
            self.work_dir = None


def _union(geoms: Iterable[BaseGeometry]) -> BaseGeometry | None:
    items = [g for g in geoms if g is not None and not g.is_empty]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    result = shapely.union_all(items)
    if result.is_empty:
        return None
    return result


def _difference(base: BaseGeometry | None, cutters: Iterable[BaseGeometry]) -> BaseGeometry | None:
    items = [g for g in cutters if g is not None and not g.is_empty]
    if base is None or base.is_empty:
        return None
    if not items:
        return base
    result = shapely.difference(base, shapely.union_all(items))
    if result.is_empty:
        return None
    return result


def _snap(geom: BaseGeometry | None) -> BaseGeometry | None:
    if geom is None or geom.is_empty:
        return None
    try:
        return shapely.set_precision(geom, GRID_MM)
    except Exception:
        return geom


def _rotate(geom: BaseGeometry, degrees: float) -> BaseGeometry:
    if not degrees:
        return geom
    return affinity.rotate(geom, degrees, origin=(0.0, 0.0))


def arc_points(
    cx: float,
    cy: float,
    radius: float,
    start_angle: float,
    end_angle: float,
    counter_clockwise: bool,
    tolerance: float = ARC_TOLERANCE_MM,
) -> list[tuple[float, float]]:
    radius = max(radius, 1e-9)
    sweep = end_angle - start_angle
    two_pi = 2.0 * math.pi
    if counter_clockwise:
        while sweep <= 0.0:
            sweep += two_pi
    else:
        while sweep >= 0.0:
            sweep -= two_pi

    cos_arg = 1.0 - tolerance / radius
    cos_arg = max(-1.0, min(1.0, cos_arg))
    max_step = 2.0 * math.acos(cos_arg)
    if max_step <= 1e-9:
        max_step = math.radians(5.0)
    segments = int(math.ceil(abs(sweep) / max_step))
    segments = max(2, min(segments, 2048))

    return [
        (
            cx + radius * math.cos(start_angle + sweep * i / segments),
            cy + radius * math.sin(start_angle + sweep * i / segments),
        )
        for i in range(1, segments + 1)
    ]


def _resolve_arc_center(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    i: float | None,
    j: float | None,
    radius: float | None,
    counter_clockwise: bool,
    multi_quadrant: bool,
) -> tuple[float, float] | None:
    if i is not None or j is not None:
        cx = x0 + (i or 0.0)
        cy = y0 + (j or 0.0)
        if multi_quadrant:
            return (cx, cy)
        best: tuple[float, float] | None = None
        best_err = float("inf")
        for sx in (1.0, -1.0):
            for sy in (1.0, -1.0):
                px = x0 + sx * abs(i or 0.0)
                py = y0 + sy * abs(j or 0.0)
                err = abs(math.hypot(px - x0, py - y0) - math.hypot(px - x1, py - y1))
                if err < best_err:
                    best_err = err
                    best = (px, py)
        return best if best_err < 1e-3 else (cx, cy)

    if radius is None:
        return None

    chord = math.hypot(x1 - x0, y1 - y0)
    if chord < 1e-12:
        return None
    radius = abs(radius)
    if chord / 2.0 > radius:
        radius = chord / 2.0

    mx = (x0 + x1) / 2.0
    my = (y0 + y1) / 2.0
    h = math.sqrt(max(radius * radius - (chord / 2.0) ** 2, 0.0))
    ux = (x1 - x0) / chord
    uy = (y1 - y0) / chord

    candidates = [(mx - uy * h, my + ux * h), (mx + uy * h, my - ux * h)]
    best = candidates[0]
    for cx, cy in candidates:
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        sweep = a1 - a0
        if counter_clockwise:
            while sweep <= 0.0:
                sweep += 2.0 * math.pi
        else:
            while sweep >= 0.0:
                sweep -= 2.0 * math.pi
        if abs(sweep) <= math.pi + 1e-6:
            best = (cx, cy)
            break
    return best


@dataclass
class CoordinateFormat:

    int_digits: int = 4
    dec_digits: int = 5
    leading_zeros_omitted: bool = True
    incremental: bool = False
    unit_scale: float = 1.0

    @property
    def total_digits(self) -> int:
        return self.int_digits + self.dec_digits

    def parse(self, text: str) -> float:
        text = text.strip()
        if not text:
            raise GerberParseError("empty coordinate")
        sign = 1.0
        if text[0] in "+-":
            if text[0] == "-":
                sign = -1.0
            text = text[1:]
        if "." in text:
            value = float(text)
        elif self.leading_zeros_omitted:
            value = int(text) / (10.0 ** self.dec_digits)
        else:
            padded = text.ljust(self.total_digits, "0")
            value = int(padded) / (10.0 ** self.dec_digits)
        return sign * value * self.unit_scale


@dataclass
class Aperture:
    code: int
    kind: str
    params: tuple[float, ...]
    geometry: BaseGeometry | None = None
    sweep_width: float = 0.1
    macro_name: str | None = None

    def at(self, x: float, y: float) -> BaseGeometry | None:
        if self.geometry is None:
            return None
        return affinity.translate(self.geometry, xoff=x, yoff=y)


def _obround(width: float, height: float) -> BaseGeometry:
    width = max(width, GRID_MM)
    height = max(height, GRID_MM)
    if abs(width - height) < 1e-9:
        return Point(0.0, 0.0).buffer(width / 2.0, quad_segs=32)
    if width > height:
        r = height / 2.0
        half = (width - height) / 2.0
        left = LineString([(-half, 0.0), (half, 0.0)])
        return left.buffer(r, cap_style="round", quad_segs=32)
    r = width / 2.0
    half = (height - width) / 2.0
    line = LineString([(0.0, -half), (0.0, half)])
    return line.buffer(r, cap_style="round", quad_segs=32)


def _regular_polygon(diameter: float, vertices: int, rotation_deg: float) -> BaseGeometry:
    vertices = max(3, int(vertices))
    radius = max(diameter, GRID_MM) / 2.0
    points = [
        (
            radius * math.cos(2.0 * math.pi * k / vertices),
            radius * math.sin(2.0 * math.pi * k / vertices),
        )
        for k in range(vertices)
    ]
    poly = Polygon(points)
    return _rotate(poly, rotation_deg)


_ALLOWED_EXPR_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Mod,
    ast.FloorDiv,
)


def eval_macro_expression(expression: str, variables: dict[str, float]) -> float:
    expression = expression.strip()
    if not expression:
        return 0.0
    rewritten = re.sub(r"\$(\d+)", r"v_\1", expression)
    if re.fullmatch(r"[0-9+\-*/().xX$ ]+", expression):
        rewritten = rewritten.replace("x", "*").replace("X", "*")
    try:
        tree = ast.parse(rewritten, mode="eval")
    except SyntaxError as exc:
        raise GerberParseError(f"bad macro expression {expression!r}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_EXPR_NODES):
            raise GerberParseError(f"disallowed macro expression {expression!r}")
    env = {f"v_{k.lstrip('$')}": v for k, v in variables.items()}
    env.update({k.lstrip("$"): v for k, v in variables.items()})
    try:
        return float(eval(compile(tree, "<macro>", "eval"), {"__builtins__": {}}, env))
    except Exception as exc:
        raise GerberParseError(f"failed to evaluate macro expression {expression!r}") from exc


@dataclass
class MacroDef:

    name: str
    statements: list[str]

    def evaluate(self, parameters: Sequence[float]) -> tuple[BaseGeometry | None, float]:
        variables: dict[str, float] = {
            f"${index + 1}": float(value) for index, value in enumerate(parameters)
        }
        additive: list[BaseGeometry] = []
        subtractive: list[BaseGeometry] = []

        for raw in self.statements:
            statement = raw.strip()
            if not statement:
                continue
            if statement.startswith("0"):
                continue
            if statement.startswith("$"):
                name, _, value = statement.partition("=")
                variables[name.strip()] = eval_macro_expression(value, variables)
                continue

            parts = [p.strip() for p in statement.split(",")]
            if not parts:
                continue
            try:
                code = int(float(parts[0]))
            except ValueError:
                continue
            numbers = [eval_macro_expression(p, variables) for p in parts[1:]]
            if not numbers:
                continue
            exposure = numbers[0]
            geom = self._primitive(code, numbers[1:])
            if geom is None or geom.is_empty:
                continue
            if exposure >= 0.5:
                additive.append(geom)
            else:
                subtractive.append(geom)

        combined = _union(additive)
        combined = _difference(combined, subtractive)
        combined = _snap(combined)
        if combined is None or combined.is_empty:
            return None, 0.1
        minx, miny, maxx, maxy = combined.bounds
        return combined, max(GRID_MM, min(maxx - minx, maxy - miny))

    @staticmethod
    def _primitive(code: int, p: Sequence[float]) -> BaseGeometry | None:
        def get(index: int, default: float = 0.0) -> float:
            return float(p[index]) if index < len(p) else default

        if code == 1:
            diameter, cx, cy, rotation = get(0, 0.01), get(1), get(2), get(3)
            geom = Point(0, 0).buffer(max(diameter, GRID_MM) / 2.0, quad_segs=32)
            return affinity.translate(_rotate(geom, rotation), xoff=cx, yoff=cy)

        if code == 4:
            count = int(get(0))
            cx, cy, rotation = get(1), get(2), get(3)
            coords = []
            for k in range(count):
                coords.append((get(4 + 2 * k), get(5 + 2 * k)))
            if len(coords) < 3:
                return None
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            return affinity.translate(_rotate(poly, rotation), xoff=cx, yoff=cy)

        if code == 5:
            vertices, cx, cy, diameter, rotation = (
                int(get(0)),
                get(1),
                get(2),
                get(3),
                get(4),
            )
            geom = _regular_polygon(diameter, vertices, rotation)
            return affinity.translate(geom, xoff=cx, yoff=cy)

        if code == 6:
            cx, cy = get(0), get(1)
            outer, ring, gap, max_rings, cross_thickness, cross_len, rotation = (
                get(2),
                get(3),
                get(4),
                int(get(5)),
                get(6),
                get(7),
                get(8),
            )
            pieces: list[BaseGeometry] = []
            diameter = outer
            rings = max(1, min(max_rings, 20))
            for _ in range(rings):
                if diameter <= 0:
                    break
                outer_ring = Point(0, 0).buffer(diameter / 2.0, quad_segs=48)
                inner = diameter / 2.0 - ring
                if inner > 0:
                    outer_ring = outer_ring.difference(
                        Point(0, 0).buffer(inner, quad_segs=48)
                    )
                pieces.append(outer_ring)
                diameter -= 2.0 * (ring + gap)
            if cross_thickness > 0 and cross_len > 0:
                half_t = cross_thickness / 2.0
                half_l = cross_len / 2.0
                pieces.append(
                    Polygon(
                        [
                            (-half_t, -half_l),
                            (half_t, -half_l),
                            (half_t, half_l),
                            (-half_t, half_l),
                        ]
                    )
                )
                pieces.append(
                    Polygon(
                        [
                            (-half_l, -half_t),
                            (half_l, -half_t),
                            (half_l, half_t),
                            (-half_l, half_t),
                        ]
                    )
                )
            geom = _union(pieces)
            if geom is None:
                return None
            return affinity.translate(_rotate(geom, rotation), xoff=cx, yoff=cy)

        if code == 7:
            cx, cy, outer, inner, gap, rotation = (
                get(0),
                get(1),
                get(2),
                get(3),
                get(4),
                get(5),
            )
            if outer <= 0:
                return None
            disc = Point(0, 0).buffer(outer / 2.0, quad_segs=48)
            if inner > 0:
                disc = disc.difference(Point(0, 0).buffer(inner / 2.0, quad_segs=48))
            if gap > 0:
                half = outer / 2.0 + 1.0
                g = max(gap, GRID_MM)
                disc = disc.difference(
                    Polygon([(-half, -g / 2), (half, -g / 2), (half, g / 2), (-half, g / 2)])
                )
                disc = disc.difference(
                    Polygon([(-g / 2, -half), (g / 2, -half), (g / 2, half), (-g / 2, half)])
                )
            return affinity.translate(_rotate(disc, rotation), xoff=cx, yoff=cy)

        if code in (20, 21, 22):
            width, height = get(0), get(1)
            if code == 20:
                x1, y1, x2, y2, rotation = get(2), get(3), get(4), get(5), get(6)
                line = LineString([(x1, y1), (x2, y2)])
                geom = line.buffer(
                    max(width, GRID_MM) / 2.0, cap_style="round", quad_segs=16
                )
            else:
                x, y, rotation = get(2), get(3), get(4)
                if code == 21:
                    x -= width / 2.0
                    y -= height / 2.0
                geom = Polygon(
                    [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
                )
            return _rotate(geom, rotation)

        return None


_WORD_RE = re.compile(r"([A-Za-z])([+-]?[0-9]+(?:\.[0-9]+)?)")
_FS_RE = re.compile(r"FS([LT])([AI])X(\d)(\d)Y(\d)(\d)")


class GerberParser:

    def __init__(self, source_name: str = "<memory>") -> None:
        self.source_name = source_name
        self.format = CoordinateFormat()
        self.apertures: dict[int, Aperture] = {}
        self.macros: dict[str, MacroDef] = {}
        self.warnings: list[str] = []

        self._current_aperture: Aperture | None = None
        self._interp = 1
        self._multi_quadrant = True
        self._region_mode = False
        self._region_points: list[tuple[float, float]] = []
        self._polarity_dark = True
        self._x = 0.0
        self._y = 0.0

        self._additive: list[BaseGeometry] = []
        self._subtractive: list[BaseGeometry] = []

        self._sr_active = False
        self._sr_x = 1
        self._sr_y = 1
        self._sr_i = 0.0
        self._sr_j = 0.0
        self._sr_buffer: list[BaseGeometry] = []

        self._finished = False


    @classmethod
    def parse_file(cls, path: Path) -> tuple[BaseGeometry | None, list[str]]:
        parser = cls(str(path))
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        parser.parse(text)
        return parser.result(), parser.warnings

    def result(self) -> BaseGeometry | None:
        self._flush_region()
        self._flush_step_repeat()
        geom = _union(self._additive)
        geom = _difference(geom, self._subtractive)
        return _snap(geom)


    def parse(self, text: str) -> None:
        index = 0
        length = len(text)
        while index < length:
            char = text[index]
            if char in "\r\n\t ":
                index += 1
                continue
            if char == "%":
                end = text.find("%", index + 1)
                if end == -1:
                    self.warnings.append("unterminated extended command block")
                    break
                self._extended(text[index + 1 : end])
                index = end + 1
            else:
                end = text.find("*", index)
                if end == -1:
                    break
                self._word(text[index:end])
                index = end + 1

    def _extended(self, block: str) -> None:
        block = block.strip()
        if block[:2].upper() == "AM":
            self._macro_definition(block[2:])
            return
        for statement in block.split("*"):
            statement = statement.strip()
            if statement:
                self._extended_statement(statement)

    def _extended_statement(self, statement: str) -> None:
        if statement.startswith("FS"):
            match = _FS_RE.match(statement)
            if not match:
                self.warnings.append(f"unrecognised FS statement: {statement!r}")
                return
            self.format.leading_zeros_omitted = match.group(1) == "L"
            self.format.incremental = match.group(2) == "I"
            self.format.int_digits = int(match.group(3))
            self.format.dec_digits = int(match.group(4))
            return

        if statement.startswith("MO"):
            unit = statement[2:4].upper()
            if unit == "IN":
                self.format.unit_scale = INCH_TO_MM
            elif unit == "MM":
                self.format.unit_scale = 1.0
            return

        if statement.startswith("AD"):
            self._aperture_definition(statement[2:])
            return

        if statement.startswith("LP"):
            self._flush_region()
            self._polarity_dark = statement[2:3].upper() != "C"
            return

        if statement.startswith("SR"):
            self._step_repeat(statement[2:])
            return

        if statement in ("IP", "IR", "OF", "MI", "SF", "IN", "LN", "TF", "TA", "TO", "TD"):
            return

        self.warnings.append(f"ignored extended command: {statement!r}")

    def _aperture_definition(self, body: str) -> None:
        name, _, param_text = body.partition(",")
        name = name.strip()
        if name[:1] in ("D", "d"):
            name = name[1:]
        params: list[float] = []
        for token in param_text.split("X"):
            token = token.strip()
            if not token:
                continue
            try:
                params.append(float(token))
            except ValueError:
                params.append(0.0)
        scale = self.format.unit_scale
        params = [p * scale for p in params]

        code_text = re.match(r"^(\d+)", name)
        if code_text is None:
            self.warnings.append(f"aperture without D-code: {body!r}")
            return
        code = int(code_text.group(1))
        kind = name[len(code_text.group(1)) :].strip()

        if kind in ("C", "R", "O", "P"):
            geometry, width = self._standard_aperture(kind, params)
            self.apertures[code] = Aperture(
                code=code, kind=kind, params=tuple(params), geometry=geometry, sweep_width=width
            )
            return

        macro = self.macros.get(kind)
        if macro is None:
            self.warnings.append(f"aperture {code} references unknown macro {kind!r}")
            self.apertures[code] = Aperture(code=code, kind="C", params=(0.1,))
            return
        geometry, width = macro.evaluate(params)
        self.apertures[code] = Aperture(
            code=code,
            kind="MACRO",
            params=tuple(params),
            geometry=geometry,
            sweep_width=width,
            macro_name=kind,
        )

    def _standard_aperture(
        self, kind: str, params: Sequence[float]
    ) -> tuple[BaseGeometry | None, float]:
        if kind == "C":
            diameter = params[0] if params else 0.1
            return Point(0, 0).buffer(max(diameter, GRID_MM) / 2.0, quad_segs=32), diameter
        if kind == "R":
            width = params[0] if len(params) > 0 else 0.1
            height = params[1] if len(params) > 1 else width
            geom = Polygon(
                [
                    (-width / 2, -height / 2),
                    (width / 2, -height / 2),
                    (width / 2, height / 2),
                    (-width / 2, height / 2),
                ]
            )
            return geom, min(width, height)
        if kind == "O":
            width = params[0] if len(params) > 0 else 0.1
            height = params[1] if len(params) > 1 else width
            return _obround(width, height), min(width, height)
        if kind == "P":
            diameter = params[0] if len(params) > 0 else 0.1
            vertices = params[1] if len(params) > 1 else 3
            rotation = params[2] if len(params) > 2 else 0.0
            return _regular_polygon(diameter, int(vertices), rotation), diameter
        return None, 0.1

    def _macro_definition(self, block: str) -> None:
        head, _, rest = block.partition("*")
        name = head.strip()
        statements = [s for s in rest.split("*") if s.strip()]
        self.macros[name] = MacroDef(name=name, statements=statements)

    def _step_repeat(self, body: str) -> None:
        self._flush_region()
        if not body:
            self._flush_step_repeat()
            return
        values = dict(re.findall(r"([XYIJ])([+-]?[0-9]*\.?[0-9]+)", body))
        self._sr_x = int(float(values.get("X", 1)))
        self._sr_y = int(float(values.get("Y", 1)))
        self._sr_i = float(values.get("I", 0.0)) * self.format.unit_scale
        self._sr_j = float(values.get("J", 0.0)) * self.format.unit_scale
        self._sr_active = True

    def _flush_step_repeat(self) -> None:
        if not self._sr_buffer:
            self._sr_active = False
            return
        replicas: list[BaseGeometry] = []
        for gx in range(max(1, self._sr_x)):
            for gy in range(max(1, self._sr_y)):
                if gx == 0 and gy == 0:
                    continue
                dx = gx * self._sr_i
                dy = gy * self._sr_j
                replicas.extend(affinity.translate(g, xoff=dx, yoff=dy) for g in self._sr_buffer)
        self._additive.extend(self._sr_buffer)
        self._additive.extend(replicas)
        self._sr_buffer = []
        self._sr_active = False


    def _word(self, word: str) -> None:
        word = word.strip()
        if not word:
            return

        if word.startswith("G04") or word.startswith("G4"):
            return

        if word.startswith("M"):
            if word[1:3] in ("02", "00", "30"):
                self._finished = True
            return

        if word.startswith("G"):
            if self._handle_g_code(word):
                return

        tokens = _WORD_RE.findall(word)
        if not tokens:
            return

        values: dict[str, str] = {}
        for letter, value in tokens:
            values[letter.upper()] = value

        d_code = None
        if "D" in values:
            try:
                d_code = int(float(values["D"]))
            except ValueError:
                d_code = None

        has_coordinates = any(k in values for k in ("X", "Y", "I", "J"))

        if not has_coordinates:
            if d_code is not None and d_code >= 10:
                self._select_aperture(d_code)
            return

        self._coordinate(values, d_code)

    def _handle_g_code(self, word: str) -> bool:
        match = re.match(r"^G(\d+)", word)
        if not match:
            return False
        code = int(match.group(1))
        rest = word[match.end() :]

        if code in (1, 2, 3):
            self._flush_region()
            self._interp = code
        elif code == 74:
            self._multi_quadrant = False
        elif code == 75:
            self._multi_quadrant = True
        elif code == 36:
            self._flush_region()
            self._region_mode = True
            self._region_points = []
        elif code == 37:
            self._flush_region()
            self._region_mode = False
        elif code == 70:
            self.format.unit_scale = INCH_TO_MM
        elif code == 71:
            self.format.unit_scale = 1.0
        elif code == 90:
            self.format.incremental = False
        elif code == 91:
            self.format.incremental = True
        elif code == 4:
            return True

        if not rest:
            return True
        if code in (1, 2, 3):
            self._word(rest)
            return True
        return True

    def _select_aperture(self, code: int) -> None:
        aperture = self.apertures.get(code)
        if aperture is None:
            self.warnings.append(f"D{code} selected before definition")
            return
        self._current_aperture = aperture


    def _coordinate(self, values: dict[str, str], d_code: int | None) -> None:
        fmt = self.format
        try:
            x_raw = fmt.parse(values["X"]) if "X" in values else None
            y_raw = fmt.parse(values["Y"]) if "Y" in values else None
            i_raw = fmt.parse(values["I"]) if "I" in values else None
            j_raw = fmt.parse(values["J"]) if "J" in values else None
            r_raw = fmt.parse(values["R"]) if "R" in values else None
        except (GerberParseError, ValueError) as exc:
            self.warnings.append(f"bad coordinate: {exc}")
            return

        if fmt.incremental:
            x = self._x + (x_raw or 0.0)
            y = self._y + (y_raw or 0.0)
        else:
            x = x_raw if x_raw is not None else self._x
            y = y_raw if y_raw is not None else self._y

        operation = d_code if d_code is not None else 1

        if operation == 2:
            self._flush_region()
            self._x, self._y = x, y
            if self._region_mode:
                self._region_points = [(x, y)]
            return

        if operation == 3:
            self._flash(x, y)
            self._x, self._y = x, y
            return

        if operation == 1:
            if self._region_mode:
                self._region_contour(x, y, i_raw, j_raw, r_raw)
            else:
                self._draw(x, y, i_raw, j_raw, r_raw)
            self._x, self._y = x, y
            return

        self.warnings.append(f"unsupported D operation D{operation:02d}")

    def _draw(
        self,
        x: float,
        y: float,
        i: float | None,
        j: float | None,
        r: float | None,
    ) -> None:
        aperture = self._current_aperture
        if aperture is None:
            self.warnings.append("draw operation with no aperture selected")
            self._x, self._y = x, y
            return

        x0, y0 = self._x, self._y
        width = max(aperture.sweep_width, GRID_MM)

        if self._interp == 1:
            segment = LineString([(x0, y0), (x, y)])
            geom = segment.buffer(width / 2.0, cap_style="round", quad_segs=16)
        else:
            center = _resolve_arc_center(
                x0,
                y0,
                x,
                y,
                i,
                j,
                r,
                counter_clockwise=self._interp == 3,
                multi_quadrant=self._multi_quadrant,
            )
            if center is None:
                segment = LineString([(x0, y0), (x, y)])
                geom = segment.buffer(width / 2.0, cap_style="round", quad_segs=16)
            else:
                cx, cy = center
                radius = math.hypot(x0 - cx, y0 - cy)
                start = math.atan2(y0 - cy, x0 - cx)
                end = math.atan2(y - cy, x - cx)
                pts = [(x0, y0)] + arc_points(
                    cx, cy, radius, start, end, counter_clockwise=self._interp == 3
                )
                if len(pts) < 2:
                    pts = [(x0, y0), (x, y)]
                geom = LineString(pts).buffer(
                    width / 2.0, cap_style="round", quad_segs=16
                )

        self._emit(geom)

    def _flash(self, x: float, y: float) -> None:
        aperture = self._current_aperture
        if aperture is None:
            self.warnings.append("flash operation with no aperture selected")
            return
        geom = aperture.at(x, y)
        if geom is None or geom.is_empty:
            self.warnings.append(f"aperture D{aperture.code} has no renderable geometry")
            return
        self._emit(geom)

    def _region_contour(
        self,
        x: float,
        y: float,
        i: float | None,
        j: float | None,
        r: float | None,
    ) -> None:
        x0, y0 = self._x, self._y
        if not self._region_points:
            self._region_points = [(x0, y0)]
        if self._interp == 1:
            self._region_points.append((x, y))
            return
        center = _resolve_arc_center(
            x0,
            y0,
            x,
            y,
            i,
            j,
            r,
            counter_clockwise=self._interp == 3,
            multi_quadrant=self._multi_quadrant,
        )
        if center is None:
            self._region_points.append((x, y))
            return
        cx, cy = center
        radius = math.hypot(x0 - cx, y0 - cy)
        start = math.atan2(y0 - cy, x0 - cx)
        end = math.atan2(y - cy, x - cx)
        self._region_points.extend(
            arc_points(cx, cy, radius, start, end, counter_clockwise=self._interp == 3)
        )

    def _flush_region(self) -> None:
        points = self._region_points
        self._region_points = []
        if len(points) < 3:
            return
        if points[0] != points[-1]:
            points.append(points[0])
        polygon = Polygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty:
            return
        self._emit(polygon)

    def _emit(self, geom: BaseGeometry) -> None:
        if geom is None or geom.is_empty:
            return
        if self._sr_active:
            self._sr_buffer.append(geom)
        elif self._polarity_dark:
            self._additive.append(geom)
        else:
            self._subtractive.append(geom)


_EXCELLON_COORD_RE = re.compile(r"([XY])([+-]?[0-9]*\.?[0-9]+)")


class ExcellonParser:

    def __init__(self, source_name: str = "<memory>", plated: bool = True) -> None:
        self.source_name = source_name
        self.plated = plated
        self.tools: dict[int, float] = {}
        self.holes: list[DrillHole] = []
        self.warnings: list[str] = []

        self._unit_scale = 1.0
        self._int_digits = 3
        self._dec_digits = 3
        self._leading_zeros = True
        self._current_tool: int | None = None
        self._in_header = False

    @classmethod
    def parse_file(cls, path: Path, plated: bool = True) -> "ExcellonParser":
        parser = cls(str(path), plated=plated)
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        parser.parse(text)
        return parser

    def parse(self, text: str) -> None:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith(";"):
                self._comment(line[1:])
                continue
            if line.startswith("%") or line in ("M95", "G05"):
                self._in_header = False
                continue
            if line.startswith("M48"):
                self._in_header = True
                continue
            if line.startswith("M30") or line.startswith("M00"):
                break

            if self._in_header:
                self._header_line(line)
            else:
                self._body_line(line)

    def _comment(self, comment: str) -> None:
        match = re.search(r"FILE_FORMAT\s*=\s*(\d+)\s*:\s*(\d+)", comment, re.IGNORECASE)
        if match:
            self._int_digits = int(match.group(1))
            self._dec_digits = int(match.group(2))
        if re.search(r"TYPE\s*=\s*NON[_ ]?PLATED", comment, re.IGNORECASE):
            self.plated = False
        if re.search(r"TYPE\s*=\s*PLATED", comment, re.IGNORECASE):
            self.plated = True

    def _header_line(self, line: str) -> None:
        upper = line.upper()
        if upper.startswith("METRIC") or upper.startswith("M71"):
            self._unit_scale = 1.0
            self._read_zeros(upper)
            return
        if upper.startswith("INCH") or upper.startswith("M72"):
            self._unit_scale = INCH_TO_MM
            self._read_zeros(upper)
            return
        if upper.startswith("FMAT") or upper.startswith("VER") or upper.startswith("ICI"):
            return
        if re.match(r"^T\d+", line):
            self._tool_definition(line)

    def _read_zeros(self, upper: str) -> None:
        if ",TZ" in upper or upper.endswith("TZ"):
            self._leading_zeros = False
        elif ",LZ" in upper:
            self._leading_zeros = True
        match = re.search(r"(\d+)\.(\d+)", upper)
        if match:
            self._int_digits = len(match.group(1))
            self._dec_digits = len(match.group(2))

    def _tool_definition(self, line: str) -> None:
        match = re.match(r"^T(\d+)(?:C([0-9]*\.?[0-9]+))?", line)
        if not match:
            return
        tool = int(match.group(1))
        if match.group(2):
            self.tools[tool] = float(match.group(2)) * self._unit_scale

    def _body_line(self, line: str) -> None:
        if line.startswith("T"):
            match = re.match(r"^T(\d+)", line)
            if match:
                self._current_tool = int(match.group(1))
                if self._current_tool not in self.tools:
                    self.tools[self._current_tool] = 0.0
                rest = line[match.end() :].strip()
                if rest:
                    self._body_line(rest)
            return

        coordinates = _EXCELLON_COORD_RE.findall(line)
        if not coordinates:
            return
        if self._current_tool is None:
            self.warnings.append(f"drill hit before tool selection: {line!r}")
            return

        values = {letter: value for letter, value in coordinates}
        try:
            x = self._parse_coordinate(values["X"]) if "X" in values else None
            y = self._parse_coordinate(values["Y"]) if "Y" in values else None
        except ValueError:
            self.warnings.append(f"bad drill coordinate: {line!r}")
            return
        if x is None and y is None:
            return

        diameter = self.tools.get(self._current_tool, 0.0)
        self.holes.append(
            DrillHole(
                x=x or 0.0,
                y=y or 0.0,
                diameter=diameter,
                plated=self.plated,
                tool=self._current_tool,
            )
        )

    def _parse_coordinate(self, text: str) -> float:
        text = text.strip()
        sign = 1.0
        if text.startswith("-"):
            sign = -1.0
            text = text[1:]
        elif text.startswith("+"):
            text = text[1:]
        if "." in text:
            return sign * float(text) * self._unit_scale
        if self._leading_zeros:
            value = int(text) / (10.0 ** self._dec_digits)
        else:
            value = int(text.ljust(self._int_digits + self._dec_digits, "0")) / (
                10.0 ** self._dec_digits
            )
        return sign * value * self._unit_scale


def _sniff_drill(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096).upper()
    except OSError:
        return False
    if "M48" in head:
        return True
    return bool(re.search(r"^(METRIC|INCH)", head, re.MULTILINE)) and bool(
        re.search(r"^T\d+C[0-9.]", head, re.MULTILINE)
    )


def classify_file(path: Path) -> tuple[LayerType, float, str]:
    name = path.name
    stem = path.stem.lower()
    suffix = path.suffix.lower()

    for pattern, layer in _NAME_SUFFIX_MAP:
        if stem.endswith(pattern):
            return layer, 0.97, f"KiCad layer suffix {pattern!r}"

    ext_layer, ext_confidence = _EXTENSION_MAP.get(suffix, (LayerType.UNKNOWN, 0.0))
    layer, confidence, reason = ext_layer, ext_confidence, f"extension {suffix!r}"

    compact = re.sub(r"[^a-z0-9]", "", stem)
    for keyword, keyword_layer in _NAME_KEYWORDS:
        if keyword.replace("_", "") in compact:
            if keyword_layer is ext_layer:
                confidence = max(confidence, 0.95)
            else:
                layer, confidence = keyword_layer, 0.85
            reason = f"filename keyword {keyword!r}"
            break

    if layer in (LayerType.DRILL_PTH, LayerType.DRILL_NPTH) and not _sniff_drill(path):
        if suffix in (".txt", ".nc"):
            return LayerType.DOCUMENTATION, 0.4, "non-drill text file"
        if suffix in _GERBER_EXTENSIONS:
            layer, confidence, reason = (
                LayerType.UNKNOWN,
                0.3,
                "Gerber extension with non-drill content",
            )

    if layer is LayerType.UNKNOWN and _sniff_drill(path):
        plated = "npth" not in compact and "nonplated" not in compact
        layer = LayerType.DRILL_PTH if plated else LayerType.DRILL_NPTH
        confidence = 0.7
        reason = "Excellon content sniff"

    return layer, confidence, reason


def _iter_archive_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def detect_layers(root: Path) -> list[tuple[Path, LayerType, float, str]]:
    results: list[tuple[Path, LayerType, float, str]] = []
    for path in _iter_archive_files(root):
        layer, confidence, reason = classify_file(path)
        if layer is LayerType.DOCUMENTATION and path.suffix.lower() in (
            ".txt",
            ".md",
            ".pdf",
            ".png",
        ):
            continue
        results.append((path, layer, confidence, reason))
    return results


def load_project(source: str | Path) -> PcbProject:
    source = Path(source).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"Gerber source not found: {source}")

    work_dir: Path | None = None
    if source.is_dir():
        root = source
    elif zipfile.is_zipfile(source):
        work_dir = Path(tempfile.mkdtemp(prefix="wegstr_gerber_"))
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                target = (work_dir / member.filename).resolve()
                if not str(target).startswith(str(work_dir.resolve())):
                    raise GerberError(f"unsafe path in archive: {member.filename!r}")
            archive.extractall(work_dir)
        root = work_dir
    else:
        raise GerberError(
            f"Unsupported source {source!r}: expected a .zip archive or a directory"
        )

    project = PcbProject(source=source, work_dir=work_dir)
    classified = detect_layers(root)

    if not classified:
        project.cleanup()
        raise GerberError(f"No Gerber or Excellon files found in {source}")

    for path, layer_type, confidence, reason in classified:
        if layer_type.is_drill:
            parser = ExcellonParser.parse_file(
                path, plated=layer_type is LayerType.DRILL_PTH
            )
            drill = DrillData(
                path=path,
                layer_type=layer_type,
                holes=parser.holes,
                tools=parser.tools,
                warnings=parser.warnings,
            )
            project.drills.append(drill)
            project.warnings.extend(f"{path.name}: {w}" for w in parser.warnings)
            continue

        if layer_type is LayerType.DOCUMENTATION:
            continue

        try:
            geometry, warnings = GerberParser.parse_file(path)
        except Exception as exc:
            project.warnings.append(f"{path.name}: failed to parse ({exc})")
            continue

        data = LayerData(
            path=path,
            layer_type=layer_type,
            confidence=confidence,
            reason=reason,
            geometry=geometry,
            warnings=warnings,
        )
        project.layers.append(data)
        project.warnings.extend(f"{path.name}: {w}" for w in warnings)

    if not project.layers and not project.drills:
        project.cleanup()
        raise GerberError(f"No parsable layers in {source}")

    return project
