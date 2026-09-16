
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import shapely
from shapely.geometry import LineString, Polygon

from gerber_io import (
    ARC_TOLERANCE_MM,
    DrillData,
    DrillHole,
    GerberError,
    LayerData,
    LayerType,
    PcbProject,
    arc_points,
)

__all__ = [
    "DxfError",
    "DxfLayerInfo",
    "list_dxf_layers",
    "load_dxf_project",
    "load_dxf_into",
]

_UNIT_SCALE = {
    0: 1.0,
    1: 25.4,
    2: 304.8,
    4: 1.0,
    5: 10.0,
    6: 1000.0,
    8: 1e-6,
    9: 1e-3,
    10: 914.4,
    11: 1e-7,
    12: 1e-6,
    13: 1e-3,
    14: 100.0,
}

_OUTLINE_KEYWORDS = (
    "edge",
    "outline",
    "board",
    "profile",
    "contour",
    "cutout",
    "panel",
    "border",
    "gko",
)

_TOP_KEYWORDS = (
    "top",
    "front",
    "copper",
    "trace",
    "signal",
    "gtl",
    "artwork",
)

_BOTTOM_KEYWORDS = ("bottom", "back", "gbl")

_SILK_KEYWORDS = ("silk", "legend", "gto", "gbo", "marking")

_DRILL_KEYWORDS = ("drill", "hole", "via", "npt", "pth")

_POINT_CODES = (10, 11, 12, 13)


class DxfError(GerberError):
    pass


@dataclass
class DxfLayerInfo:

    name: str
    role: str
    confidence: float
    reason: str
    paths: int = 0
    circles: int = 0
    closed: int = 0
    width: float = 0.0


@dataclass
class _Raw:

    kind: str
    attrs: list[tuple[int, str]] = field(default_factory=list)
    vertices: list["_Raw"] = field(default_factory=list)

    def first(self, code: int, default: Any = None) -> Any:
        for key, value in self.attrs:
            if key == code:
                return value
        return default

    def number(self, code: int, default: float = 0.0) -> float:
        raw = self.first(code)
        if raw is None:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def text(self, code: int, default: str = "") -> str:
        raw = self.first(code)
        return default if raw is None else str(raw)


def _pairs(text: str) -> Iterator[tuple[int, str]]:
    lines = text.splitlines()
    index = 0
    total = len(lines)
    while index + 1 < total:
        code_text = lines[index].strip()
        value = lines[index + 1].strip()
        index += 2
        if not code_text:
            continue
        try:
            code = int(code_text)
        except ValueError:
            continue
        yield code, value


def _read_entities(
    pairs: list[tuple[int, str]], index: int, stop_at: str
) -> tuple[list[_Raw], int]:
    flat: list[_Raw] = []
    total = len(pairs)
    while index < total:
        code, value = pairs[index]
        index += 1
        if code == 0:
            if value == stop_at:
                break
            flat.append(_Raw(kind=value))
            continue
        if flat:
            flat[-1].attrs.append((code, value))

    entities: list[_Raw] = []
    cursor = 0
    count = len(flat)
    while cursor < count:
        item = flat[cursor]
        if item.kind == "POLYLINE":
            vertices: list[_Raw] = []
            cursor += 1
            while cursor < count and flat[cursor].kind == "VERTEX":
                vertices.append(flat[cursor])
                cursor += 1
            if cursor < count and flat[cursor].kind == "SEQEND":
                cursor += 1
            item.vertices = vertices
            entities.append(item)
            continue
        if item.kind in ("VERTEX", "SEQEND"):
            cursor += 1
            continue
        entities.append(item)
        cursor += 1
    return entities, index


def _block_entities(
    pairs: list[tuple[int, str]], index: int
) -> tuple[dict[str, tuple[tuple[float, float], list[_Raw]]], int]:
    blocks: dict[str, tuple[tuple[float, float], list[_Raw]]] = {}
    total = len(pairs)
    while index < total:
        code, value = pairs[index]
        index += 1
        if code != 0:
            continue
        if value == "ENDSEC":
            break
        if value != "BLOCK":
            continue
        header = _Raw(kind="BLOCK")
        while index < total:
            code2, value2 = pairs[index]
            if code2 == 0:
                break
            header.attrs.append((code2, value2))
            index += 1
        name = header.text(2).strip()
        base = (header.number(10), header.number(20))
        body, index = _read_entities(pairs, index, "ENDBLK")
        if name:
            blocks[name] = (base, body)
    return blocks, index


def _layer_widths(pairs: list[tuple[int, str]], index: int) -> tuple[dict[str, float], int]:
    widths: dict[str, float] = {}
    total = len(pairs)
    while index < total:
        code, value = pairs[index]
        index += 1
        if code != 0:
            continue
        if value == "ENDSEC":
            break
        if value != "LAYER":
            continue
        record = _Raw(kind="LAYER")
        while index < total:
            code2, value2 = pairs[index]
            if code2 == 0:
                break
            record.attrs.append((code2, value2))
            index += 1
        name = record.text(2).strip()
        lineweight = record.number(370)
        if name and lineweight > 0:
            widths[name] = lineweight / 100.0
    return widths, index


def _de_boor(
    points: Sequence[tuple[float, float]],
    weights: Sequence[float],
    knots: Sequence[float],
    degree: int,
    t: float,
) -> tuple[float, float]:
    n = len(points) - 1
    span = degree
    if t >= knots[n + 1]:
        span = n
    elif t > knots[degree]:
        for i in range(degree, n + 1):
            if knots[i] <= t < knots[i + 1]:
                span = i
                break

    d: list[list[float]] = []
    for j in range(degree + 1):
        index = max(0, min(n, span - degree + j))
        w = weights[index]
        d.append([points[index][0] * w, points[index][1] * w, w])

    for r in range(1, degree + 1):
        for j in range(degree, r - 1, -1):
            index = span - degree + j
            denom = knots[index + degree - r + 1] - knots[index]
            alpha = 0.0 if abs(denom) < 1e-12 else (t - knots[index]) / denom
            for k in range(3):
                d[j][k] = (1.0 - alpha) * d[j - 1][k] + alpha * d[j][k]

    w = d[degree][2]
    if abs(w) < 1e-12:
        return (d[degree][0], d[degree][1])
    return (d[degree][0] / w, d[degree][1] / w)


def _dedupe(
    points: Iterable[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for point in points:
        if result:
            last = result[-1]
            if math.hypot(point[0] - last[0], point[1] - last[1]) <= tolerance:
                continue
        result.append(point)
    return result


def _spline_points(raw: _Raw, tolerance: float) -> list[tuple[float, float]]:
    xs = [float(v) for c, v in raw.attrs if c == 10]
    ys = [float(v) for c, v in raw.attrs if c == 20]
    points = list(zip(xs, ys))
    if len(points) < 2:
        return points

    weights = [float(v) for c, v in raw.attrs if c == 42]
    if len(weights) != len(points):
        weights = [1.0] * len(points)

    degree = int(raw.number(71, 3.0))
    degree = max(1, min(degree, len(points) - 1))

    knots = [float(v) for c, v in raw.attrs if c == 40]
    if len(knots) != len(points) + degree + 1:
        knots = [0.0] * (degree + 1)
        inner = len(points) - degree - 1
        for i in range(1, inner + 1):
            knots.append(i / (inner + 1))
        knots.extend([1.0] * (degree + 1))

    start = knots[degree]
    end = knots[len(knots) - degree - 1]
    if end <= start:
        return points

    steps = max(8, min(512, int((end - start) * 64) + 8))
    samples = [
        _de_boor(points, weights, knots, degree, start + (end - start) * i / steps)
        for i in range(steps + 1)
    ]
    return _dedupe(samples, tolerance)


def _bulge_points(
    p0: tuple[float, float],
    p1: tuple[float, float],
    bulge: float,
    tolerance: float,
) -> list[tuple[float, float]]:
    if abs(bulge) < 1e-12:
        return []
    theta = 4.0 * math.atan(bulge)
    chord = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    if chord < 1e-12 or abs(math.sin(theta / 2.0)) < 1e-12:
        return []
    radius = abs(chord / (2.0 * math.sin(theta / 2.0)))
    mx = (p0[0] + p1[0]) / 2.0
    my = (p0[1] + p1[1]) / 2.0
    ux = (p1[0] - p0[0]) / chord
    uy = (p1[1] - p0[1]) / chord
    h = math.sqrt(max(radius * radius - (chord / 2.0) ** 2, 0.0))
    sign = 1.0 if bulge > 0 else -1.0
    cx = mx + sign * h * (-uy)
    cy = my + sign * h * ux
    start = math.atan2(p0[1] - cy, p0[0] - cx)
    end = math.atan2(p1[1] - cy, p1[0] - cx)
    return arc_points(cx, cy, radius, start, end, bulge > 0, tolerance)


def _circle_points(raw: _Raw, tolerance: float) -> list[tuple[float, float]]:
    radius = raw.number(40)
    if radius <= 0:
        return []
    return arc_points(
        raw.number(10), raw.number(20), radius, 0.0, 2.0 * math.pi, True, tolerance
    )


def _arc_points(raw: _Raw, tolerance: float) -> list[tuple[float, float]]:
    radius = raw.number(40)
    if radius <= 0:
        return []
    start = math.radians(raw.number(50))
    end = math.radians(raw.number(51))
    return arc_points(
        raw.number(10), raw.number(20), radius, start, end, True, tolerance
    )


def _ellipse_points(raw: _Raw, tolerance: float) -> list[tuple[float, float]]:
    cx = raw.number(10)
    cy = raw.number(20)
    mx = raw.number(11)
    my = raw.number(21)
    ratio = raw.number(40, 1.0)
    start = raw.number(41, 0.0)
    end = raw.number(42, 0.0)
    if end <= start:
        end = start + 2.0 * math.pi
    major = math.hypot(mx, my)
    if major < 1e-12:
        return []
    ux, uy = mx / major, my / major
    minor = ratio * major
    sweep = end - start
    step = max(tolerance / max(major, 1e-9), 1e-3)
    steps = max(8, min(2048, int(abs(sweep) / step) + 2))
    return [
        (
            cx + major * math.cos(start + sweep * i / steps) * ux
            - minor * math.sin(start + sweep * i / steps) * uy,
            cy + major * math.cos(start + sweep * i / steps) * uy
            + minor * math.sin(start + sweep * i / steps) * ux,
        )
        for i in range(steps + 1)
    ]


def _polyline_vertices(raw: _Raw) -> list[tuple[float, float, float]]:
    vertices: list[tuple[float, float, float]] = []
    if raw.kind == "LWPOLYLINE":
        x = None
        bulge = 0.0
        for code, value in raw.attrs:
            if code == 10:
                if x is not None:
                    vertices.append((x, y, bulge))
                try:
                    x = float(value)
                except ValueError:
                    x = None
                y = 0.0
                bulge = 0.0
            elif code == 20 and x is not None:
                try:
                    y = float(value)
                except ValueError:
                    y = 0.0
            elif code == 42 and x is not None:
                try:
                    bulge = float(value)
                except ValueError:
                    bulge = 0.0
        if x is not None:
            vertices.append((x, y, bulge))
        return vertices

    for vertex in raw.vertices:
        vertices.append((vertex.number(10), vertex.number(20), vertex.number(42)))
    return vertices


def _entity_path(
    raw: _Raw, tolerance: float
) -> tuple[list[tuple[float, float]], bool] | None:
    kind = raw.kind
    if kind == "LINE":
        return (
            [
                (raw.number(10), raw.number(20)),
                (raw.number(11), raw.number(21)),
            ],
            False,
        )

    if kind in ("LWPOLYLINE", "POLYLINE"):
        vertices = _polyline_vertices(raw)
        if len(vertices) < 2:
            return None
        closed = bool(int(raw.number(70)) & 1)
        points: list[tuple[float, float]] = []
        for index, (x, y, bulge) in enumerate(vertices):
            points.append((x, y))
            if abs(bulge) < 1e-12:
                continue
            if index + 1 < len(vertices):
                nx, ny = vertices[index + 1][0], vertices[index + 1][1]
            elif closed:
                nx, ny = vertices[0][0], vertices[0][1]
            else:
                continue
            points.extend(_bulge_points((x, y), (nx, ny), bulge, tolerance))
        return (_dedupe(points, tolerance), closed)

    if kind == "CIRCLE":
        return (_circle_points(raw, tolerance), True)

    if kind == "ARC":
        return (_arc_points(raw, tolerance), False)

    if kind == "ELLIPSE":
        return (_ellipse_points(raw, tolerance), False)

    if kind == "SPLINE":
        points = _spline_points(raw, tolerance)
        if len(points) < 2:
            return None
        return (points, bool(int(raw.number(70)) & 1))

    if kind in ("SOLID", "TRACE", "3DFACE"):
        corners: list[tuple[float, float]] = []
        for code in _POINT_CODES:
            if raw.first(code) is None:
                continue
            corners.append((raw.number(code), raw.number(code + 10)))
        if len(corners) < 3:
            return None
        if len(corners) == 4 and corners[2] == corners[3]:
            corners.pop()
        return (corners, True)

    return None


def _is_closed(raw: _Raw) -> bool:
    if raw.kind == "CIRCLE":
        return True
    if raw.kind in ("LWPOLYLINE", "POLYLINE"):
        return bool(int(raw.number(70)) & 1)
    return raw.kind in ("SOLID", "TRACE", "3DFACE")


def _transform_point(
    x: float,
    y: float,
    base: tuple[float, float],
    scale: tuple[float, float],
    rotation: float,
    offset: tuple[float, float],
) -> tuple[float, float]:
    dx = (x - base[0]) * scale[0]
    dy = (y - base[1]) * scale[1]
    if rotation:
        cos_r = math.cos(rotation)
        sin_r = math.sin(rotation)
        dx, dy = dx * cos_r - dy * sin_r, dx * sin_r + dy * cos_r
    return (offset[0] + dx, offset[1] + dy)


def _transform_vector(
    x: float, y: float, scale: tuple[float, float], rotation: float
) -> tuple[float, float]:
    dx = x * scale[0]
    dy = y * scale[1]
    if rotation:
        cos_r = math.cos(rotation)
        sin_r = math.sin(rotation)
        dx, dy = dx * cos_r - dy * sin_r, dx * sin_r + dy * cos_r
    return (dx, dy)


def _rewrite_points(
    raw: _Raw,
    codes: Sequence[int],
    base: tuple[float, float],
    scale: tuple[float, float],
    rotation: float,
    offset: tuple[float, float],
    as_vector: bool = False,
) -> list[tuple[int, str]]:
    attrs = list(raw.attrs)
    result: list[tuple[int, str]] = []
    index = 0
    total = len(attrs)
    while index < total:
        code, value = attrs[index]
        if code in codes and index + 1 < total and attrs[index + 1][0] == code + 10:
            try:
                x = float(value)
                y = float(attrs[index + 1][1])
            except (TypeError, ValueError):
                result.append(attrs[index])
                index += 1
                continue
            if as_vector:
                nx, ny = _transform_vector(x, y, scale, rotation)
            else:
                nx, ny = _transform_point(x, y, base, scale, rotation, offset)
            result.append((code, repr(nx)))
            result.append((code + 10, repr(ny)))
            index += 2
            continue
        result.append((code, value))
        index += 1
    return result


def _scale_attr(attrs: list[tuple[int, str]], code: int, factor: float) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for key, value in attrs:
        if key == code:
            try:
                result.append((key, repr(float(value) * factor)))
                continue
            except (TypeError, ValueError):
                pass
        result.append((key, value))
    return result


def _shift_attr(attrs: list[tuple[int, str]], code: int, delta: float) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for key, value in attrs:
        if key == code:
            try:
                result.append((key, repr(float(value) + delta)))
                continue
            except (TypeError, ValueError):
                pass
        result.append((key, value))
    return result


def _transform_raw(
    raw: _Raw,
    base: tuple[float, float],
    scale: tuple[float, float],
    rotation: float,
    offset: tuple[float, float],
) -> _Raw:
    kind = raw.kind
    average = (abs(scale[0]) + abs(scale[1])) / 2.0

    if kind == "ELLIPSE":
        attrs = _rewrite_points(raw, (10,), base, scale, rotation, offset)
        attrs = _rewrite_points(
            _Raw(kind, attrs), (11,), base, scale, rotation, offset, as_vector=True
        )
        return _Raw(kind, attrs)

    attrs = _rewrite_points(raw, _POINT_CODES, base, scale, rotation, offset)

    if kind in ("CIRCLE", "ARC"):
        attrs = _scale_attr(attrs, 40, average)
    if kind == "ARC" and rotation:
        degrees = math.degrees(rotation)
        attrs = _shift_attr(attrs, 50, degrees)
        attrs = _shift_attr(attrs, 51, degrees)

    vertices: list[_Raw] = []
    for vertex in raw.vertices:
        vertex_attrs = _rewrite_points(
            vertex, (10,), base, scale, rotation, offset
        )
        vertices.append(_Raw(vertex.kind, vertex_attrs))

    return _Raw(kind, attrs, vertices)


def _expand_insert(
    raw: _Raw,
    blocks: dict[str, tuple[tuple[float, float], list[_Raw]]],
    depth: int = 0,
) -> list[_Raw]:
    if depth > 8:
        return []
    name = raw.text(2).strip()
    entry = blocks.get(name)
    if entry is None:
        return []
    base, body = entry
    ix = raw.number(10)
    iy = raw.number(20)
    sx = raw.number(41, 1.0) or 1.0
    sy = raw.number(42, 1.0) or 1.0
    rotation = math.radians(raw.number(50))
    columns = max(1, int(raw.number(70, 1.0)))
    rows = max(1, int(raw.number(71, 1.0)))
    col_spacing = raw.number(44)
    row_spacing = raw.number(45)

    results: list[_Raw] = []
    for row in range(rows):
        for col in range(columns):
            offset = (ix + col * col_spacing, iy + row * row_spacing)
            for child in body:
                if child.kind == "INSERT":
                    for nested in _expand_insert(child, blocks, depth + 1):
                        results.append(
                            _transform_raw(nested, base, (sx, sy), rotation, offset)
                        )
                    continue
                results.append(
                    _transform_raw(child, base, (sx, sy), rotation, offset)
                )
    return results


def _entity_width(raw: _Raw) -> float | None:
    value = raw.number(370)
    if value > 0:
        return value / 100.0
    return None


def _parse_file(path: Path) -> tuple[list[_Raw], dict[str, float], float]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DxfError(f"Could not read {path.name}: {exc}") from exc

    if "AutoCAD Binary DXF" in text[:64]:
        raise DxfError(f"{path.name} is a binary DXF, which is not supported.")

    blocks: dict[str, tuple[tuple[float, float], list[_Raw]]] = {}
    layer_widths: dict[str, float] = {}
    insunits = 4.0
    entities: list[_Raw] = []

    pairs = list(_pairs(text))
    total = len(pairs)
    index = 0
    while index < total:
        code, value = pairs[index]
        index += 1
        if code != 0:
            continue
        if value == "EOF":
            break
        if value != "SECTION":
            continue
        if index >= total:
            break
        section_code, section_name = pairs[index]
        index += 1
        if section_code != 2:
            continue

        if section_name == "HEADER":
            while index < total:
                code2, value2 = pairs[index]
                index += 1
                if code2 == 0 and value2 == "ENDSEC":
                    break
                if code2 == 9 and value2 == "$INSUNITS" and index < total:
                    if pairs[index][0] == 70:
                        try:
                            insunits = float(pairs[index][1] or 4)
                        except ValueError:
                            insunits = 4.0
                        index += 1
        elif section_name == "TABLES":
            layer_widths, index = _layer_widths(pairs, index)
        elif section_name == "BLOCKS":
            blocks, index = _block_entities(pairs, index)
        elif section_name == "ENTITIES":
            entities, index = _entities_from(pairs, index, blocks)

    scale = _UNIT_SCALE.get(int(insunits), 1.0)
    return entities, layer_widths, scale


def _entities_from(
    pairs: list[tuple[int, str]], index: int, blocks: dict[str, tuple[tuple[float, float], list[_Raw]]]
) -> tuple[list[_Raw], int]:
    raw_entities, index = _read_entities(pairs, index, "ENDSEC")
    expanded: list[_Raw] = []
    for raw in raw_entities:
        if raw.kind == "INSERT":
            expanded.extend(_expand_insert(raw, blocks))
        else:
            expanded.append(raw)

    results: list[_Raw] = []
    for raw in expanded:
        if int(raw.number(67)) == 1:
            continue
        if raw.kind in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "LEADER"):
            continue
        results.append(raw)
    return results, index


def _layer_geometry(
    entities: Sequence[_Raw], default_width: float, tolerance: float
) -> Any:
    polygons: list[Any] = []
    lines: list[LineString] = []
    width = 0.0

    for entity in entities:
        parsed = _entity_path(entity, tolerance)
        if parsed is None:
            continue
        points, closed = parsed
        if len(points) < 2:
            continue
        entity_width = _entity_width(entity)
        if entity_width:
            width = max(width, entity_width)
        if closed and len(points) >= 3:
            polygon = Polygon(points)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.is_empty:
                continue
            polygons.append(polygon)
            continue
        line = LineString(points)
        if not line.is_empty:
            lines.append(line)

    pieces: list[Any] = list(polygons)
    if lines:
        effective = width if width > 0 else default_width
        half = max(effective, 1e-4) / 2.0
        merged = shapely.union_all(lines)
        pieces.append(
            merged.buffer(half, quad_segs=8, cap_style="round", join_style="round")
        )

    if not pieces:
        return None
    merged = shapely.union_all(pieces)
    if merged.is_empty:
        return None
    return merged


def _guess_role(
    name: str, geometry: Any, circle_count: int
) -> tuple[LayerType, float, str]:
    compact = "".join(ch for ch in name.lower() if ch.isalnum())

    if any(key in compact for key in _DRILL_KEYWORDS) and circle_count:
        plated = "npth" not in compact and "nonplated" not in compact
        role = LayerType.DRILL_PTH if plated else LayerType.DRILL_NPTH
        return role, 0.8, f"layer name {name!r} looks like drill data"

    if any(key in compact for key in _SILK_KEYWORDS):
        role = LayerType.BOTTOM_SILK if "bottom" in compact else LayerType.TOP_SILK
        return role, 0.85, f"layer name {name!r} looks like silkscreen"

    if any(key in compact for key in _OUTLINE_KEYWORDS):
        return LayerType.EDGE_CUTS, 0.9, f"layer name {name!r} looks like the outline"

    if any(key in compact for key in _BOTTOM_KEYWORDS):
        return LayerType.BOTTOM_COPPER, 0.8, f"layer name {name!r} looks like back copper"

    if any(key in compact for key in _TOP_KEYWORDS):
        return LayerType.TOP_COPPER, 0.8, f"layer name {name!r} looks like front copper"

    if geometry is None or getattr(geometry, "is_empty", True):
        return LayerType.UNKNOWN, 0.0, "empty layer"

    parts = list(getattr(geometry, "geoms", [geometry]))
    if len(parts) == 1 and parts[0].geom_type == "Polygon":
        part = parts[0]
        thickness = part.area / max(part.length, 1e-9)
        if thickness > 1.0:
            return (
                LayerType.EDGE_CUTS,
                0.55,
                "a single chunky closed region, so it is treated as the outline",
            )
    return LayerType.TOP_COPPER, 0.45, "defaulted to front copper"


def _layer_type_from_text(text: str) -> LayerType:
    try:
        return LayerType(text)
    except ValueError:
        pass
    compact = "".join(ch for ch in text.lower() if ch.isalnum())
    if any(key in compact for key in _OUTLINE_KEYWORDS):
        return LayerType.EDGE_CUTS
    if any(key in compact for key in _SILK_KEYWORDS):
        return LayerType.TOP_SILK
    if "bottom" in compact or "back" in compact:
        return LayerType.BOTTOM_COPPER
    if any(key in compact for key in _DRILL_KEYWORDS):
        return LayerType.DRILL_PTH
    return LayerType.TOP_COPPER


def load_dxf_into(
    project: PcbProject,
    path: Path,
    *,
    default_width: float = 0.2,
    roles: dict[str, str] | None = None,
    tolerance: float = ARC_TOLERANCE_MM,
) -> list[DxfLayerInfo]:
    entities, layer_widths, scale = _parse_file(path)
    if not entities:
        project.warnings.append(f"{path.name}: no drawable DXF entities found.")
        return []

    if abs(scale - 1.0) > 1e-12:
        identity = (0.0, 0.0)
        entities = [
            _transform_raw(raw, identity, (scale, scale), 0.0, identity)
            for raw in entities
        ]

    grouped: dict[str, list[_Raw]] = {}
    for entity in entities:
        layer = entity.text(8).strip() or "0"
        grouped.setdefault(layer, []).append(entity)

    infos: list[DxfLayerInfo] = []
    role_overrides = {k.lower(): v for k, v in (roles or {}).items()}

    for name, items in grouped.items():
        width = layer_widths.get(name, default_width)
        geometry = _layer_geometry(items, width, tolerance)
        circles = [e for e in items if e.kind == "CIRCLE" and e.number(40) > 0]

        override = role_overrides.get(name.lower())
        if override:
            layer_type = _layer_type_from_text(override)
            confidence, reason = 0.99, f"role set to {override!r} by the caller"
        else:
            layer_type, confidence, reason = _guess_role(name, geometry, len(circles))

        if layer_type.is_drill:
            holes = [
                DrillHole(
                    x=entity.number(10),
                    y=entity.number(20),
                    diameter=entity.number(40) * 2.0,
                    plated=layer_type is LayerType.DRILL_PTH,
                )
                for entity in circles
            ]
            if holes:
                project.drills.append(
                    DrillData(
                        path=path,
                        layer_type=layer_type,
                        holes=holes,
                        tools={},
                        warnings=[],
                    )
                )
            infos.append(
                DxfLayerInfo(
                    name=name,
                    role=layer_type.value,
                    confidence=confidence,
                    reason=reason,
                    circles=len(circles),
                    width=width,
                )
            )
            continue

        if layer_type in (LayerType.DOCUMENTATION, LayerType.UNKNOWN):
            continue

        if geometry is None or geometry.is_empty:
            project.warnings.append(
                f"{path.name}: DXF layer {name!r} had no usable geometry."
            )
            continue

        project.layers.append(
            LayerData(
                path=path,
                layer_type=layer_type,
                confidence=confidence,
                reason=f"DXF layer {name!r}: {reason}",
                geometry=geometry,
                warnings=[],
            )
        )
        infos.append(
            DxfLayerInfo(
                name=name,
                role=layer_type.value,
                confidence=confidence,
                reason=reason,
                paths=len(items),
                circles=len(circles),
                closed=sum(1 for entity in items if _is_closed(entity)),
                width=width,
            )
        )

    if not infos:
        project.warnings.append(f"{path.name}: no usable DXF layers.")
    return infos


def list_dxf_layers(path: str | Path) -> list[dict[str, Any]]:
    resolved = Path(path).expanduser()
    entities, layer_widths, scale = _parse_file(resolved)
    grouped: dict[str, int] = {}
    for entity in entities:
        layer = entity.text(8).strip() or "0"
        grouped[layer] = grouped.get(layer, 0) + 1
    return [
        {
            "name": name,
            "entities": count,
            "width": layer_widths.get(name, 0.0),
            "units_scale": scale,
        }
        for name, count in grouped.items()
    ]


def load_dxf_project(
    source: str | Path,
    *,
    default_width: float = 0.2,
    roles: dict[str, str] | None = None,
) -> PcbProject:
    resolved = Path(source).expanduser()
    if not resolved.is_file():
        raise DxfError(f"DXF file not found: {resolved}")

    project = PcbProject(source=resolved)
    load_dxf_into(project, resolved, default_width=default_width, roles=roles)
    if not project.layers and not project.drills:
        raise DxfError(f"No usable geometry found in {resolved.name}")
    return project
