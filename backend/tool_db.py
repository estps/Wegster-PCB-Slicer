from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from pcb_engine import ToolSpec
from sqlite_read import SqliteError, SqliteReader

DB_FILENAME = "Cut2D_tools_database.vtdb"

_TOOL_TYPE_KIND = {
    1: "flat",
    2: "flat",
    3: "vbit",
    4: "vbit",
}

_KIND_LABEL = {
    "flat": "End mill",
    "vbit": "Engraving bit",
    "drill": "Drill",
}


class ToolDbError(Exception):
    pass


@dataclass
class DbTool:

    key: str
    name: str
    group: str = ""
    kind: str = "flat"
    diameter: float = 0.0
    angle: float = 0.0
    tip_diameter: float = 0.0
    flutes: int = 0
    tool_number: int = 0
    feed_rate: float = 0.0
    plunge_rate: float = 0.0
    spindle_speed: int = 0
    stepdown: float = 0.0
    stepover: float = 0.0
    clear_stepover: float = 0.0
    notes: str = ""

    @property
    def label(self) -> str:
        return self.name

    @property
    def group_label(self) -> str:
        return self.group or _KIND_LABEL.get(self.kind, "Tools")

    def cut_width(self, depth: float) -> float:
        if self.kind == "vbit":
            return self.tip_diameter + 2.0 * abs(depth) * _tan_half(self.angle)
        return self.diameter

    def to_tool_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            kind=self.kind,
            diameter=self.diameter,
            tip_diameter=self.tip_diameter,
            angle=self.angle if self.kind == "vbit" else 0.0,
            feed_rate=self.feed_rate,
            plunge_rate=self.plunge_rate,
            spindle_rpm=self.spindle_speed,
            stepdown=self.stepdown,
            stepover=self.stepover,
            source=self.key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "group": self.group_label,
            "kind": self.kind,
            "kind_label": _KIND_LABEL.get(self.kind, self.kind),
            "diameter": round(self.diameter, 4),
            "angle": round(self.angle, 3),
            "tip_diameter": round(self.tip_diameter, 4),
            "flutes": self.flutes,
            "tool_number": self.tool_number,
            "feed_rate": self.feed_rate,
            "plunge_rate": self.plunge_rate,
            "spindle_speed": self.spindle_speed,
            "stepdown": self.stepdown,
            "stepover": self.stepover,
            "clear_stepover": self.clear_stepover,
            "notes": self.notes,
        }


@dataclass
class ToolDatabase:

    path: Path | None = None
    machine: str = ""
    material: str = ""
    tools: list[DbTool] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.tools)

    def __iter__(self):
        return iter(self.tools)

    def by_key(self, key: str | None) -> DbTool | None:
        if not key:
            return None
        for tool in self.tools:
            if tool.key == key:
                return tool
        return None

    def by_name(self, name: str | None) -> DbTool | None:
        if not name:
            return None
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def flats(self) -> list[DbTool]:
        return sorted(
            (t for t in self.tools if t.kind == "flat"), key=lambda t: t.diameter
        )

    def vbits(self) -> list[DbTool]:
        return sorted(
            (t for t in self.tools if t.kind == "vbit"),
            key=lambda t: (t.angle, t.diameter),
        )

    def groups(self) -> dict[str, list[DbTool]]:
        result: dict[str, list[DbTool]] = {}
        for tool in self.tools:
            result.setdefault(tool.group_label, []).append(tool)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "machine": self.machine,
            "material": self.material,
            "count": len(self.tools),
            "warnings": self.warnings,
            "groups": [
                {"name": name, "tools": [t.to_dict() for t in items]}
                for name, items in self.groups().items()
            ],
            "tools": [t.to_dict() for t in self.tools],
        }


def _tan_half(angle: float) -> float:
    import math

    return math.tan(math.radians(max(0.0, angle) / 2.0))


def _cheap_candidates() -> list[Path]:
    seen: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser()
        except Exception:
            return
        if resolved not in seen:
            seen.append(resolved)

    env = os.environ.get("WEGSTR_TOOL_DB")
    if env:
        add(Path(env))

    documents = Path.home() / "Documents"
    add(documents / DB_FILENAME)

    try:
        for found in sorted(documents.glob("*.vtdb")):
            add(found)
    except OSError:
        pass

    return seen


def _deep_candidates() -> list[Path]:
    seen: list[Path] = []
    for base in (
        Path(os.environ.get("APPDATA", str(Path.home()))) / "Vectric",
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Vectric",
        Path.home() / "Documents" / "Vectric",
    ):
        try:
            if base.is_dir():
                for found in sorted(base.glob("**/" + DB_FILENAME)):
                    if found not in seen:
                        seen.append(found)
        except OSError:
            continue
    return seen


def find_tool_db(deep: bool = True) -> Path | None:
    for path in _cheap_candidates():
        if path.is_file():
            return path
    if not deep:
        return None
    for path in _deep_candidates():
        if path.is_file():
            return path
    return None


def _read_database(path: Path) -> SqliteReader:
    try:
        return SqliteReader(path)
    except SqliteError as exc:
        raise ToolDbError(str(exc)) from exc


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _group_names(reader: SqliteReader) -> dict[str, str]:
    if not reader.has_table("tool_tree_entry"):
        return {}
    entries = reader.rows("tool_tree_entry")
    names = {_text(row.get("id")): _text(row.get("name")) for row in entries}
    mapping: dict[str, str] = {}
    for row in entries:
        geometry = _text(row.get("tool_geometry_id"))
        if not geometry:
            continue
        group = ""
        parent = _text(row.get("parent_group_id"))
        guard = 0
        while parent and guard < 12:
            label = names.get(parent, "")
            if label:
                group = label
                break
            node = next((e for e in entries if _text(e.get("id")) == parent), None)
            parent = _text(node.get("parent_group_id")) if node else ""
            guard += 1
        mapping[geometry] = group
    return mapping


def _best_cutting_rows(reader: SqliteReader) -> dict[str, dict[str, Any]]:
    if not reader.has_table("tool_entity") or not reader.has_table("tool_cutting_data"):
        return {}

    cutting = {_text(row.get("id")): row for row in reader.rows("tool_cutting_data")}
    joined: list[tuple[str, dict[str, Any]]] = []
    for entity in reader.rows("tool_entity"):
        geometry = _text(entity.get("tool_geometry_id"))
        data = cutting.get(_text(entity.get("tool_cutting_data_id")))
        if geometry and data is not None:
            joined.append((geometry, data))

    best: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for geometry, row in joined:
        score = sum(
            1.0
            for key in ("feed_rate", "plunge_rate", "spindle_speed", "stepdown", "stepover")
            if row.get(key) is not None
        )
        if _number(row.get("spindle_speed")) > 0:
            score += 1.0
        if geometry not in best or score > scores[geometry]:
            best[geometry] = row
            scores[geometry] = score
    return best


def load_tool_db(path: str | Path | None = None) -> ToolDatabase:
    resolved = Path(path).expanduser() if path else find_tool_db()
    if resolved is None or not resolved.is_file():
        raise ToolDbError(
            "No Vectric tool database found. Set WEGSTR_TOOL_DB or place "
            f"{DB_FILENAME} in your Documents folder."
        )

    reader = _read_database(resolved)
    try:
        if not reader.has_table("tool_geometry"):
            raise ToolDbError(f"{resolved.name} is not a Vectric tool database.")

        groups = _group_names(reader)
        cutting = _best_cutting_rows(reader)

        database = ToolDatabase(path=resolved)
        database.machine = _first_value(reader, "machine", ("name", "model"))
        database.material = _first_value(reader, "material", ("name",))

        for row in reader.rows("tool_geometry"):
            kind = _TOOL_TYPE_KIND.get(int(_number(row.get("tool_type"), 1.0)), "flat")
            units = int(_number(row.get("units"), 0.0))
            diameter = _number(row.get("diameter"))
            if units in (1, 2):
                diameter *= 25.4
            angle = _number(row.get("included_angle"))
            tip = _number(row.get("flat_diameter"))
            if kind == "vbit" and angle <= 0.0:
                kind = "flat"
            if kind == "flat" and diameter <= 0.0:
                continue

            key = _text(row.get("id"))
            name = _text(row.get("name_format")).strip() or f"{diameter:.3f} mm tool"
            tool = DbTool(
                key=key,
                name=name,
                group=groups.get(key, ""),
                kind=kind,
                diameter=round(diameter, 4),
                angle=round(angle, 3),
                tip_diameter=round(tip, 4),
                flutes=int(_number(row.get("num_flutes"))),
                notes=_text(row.get("notes")).strip(),
            )

            data = cutting.get(key)
            if data is not None:
                tool.tool_number = int(_number(data.get("tool_number")))
                tool.feed_rate = _number(data.get("feed_rate"))
                tool.plunge_rate = _number(data.get("plunge_rate"))
                tool.spindle_speed = int(_number(data.get("spindle_speed")))
                tool.stepdown = _number(data.get("stepdown"))
                tool.stepover = _number(data.get("stepover"))
                tool.clear_stepover = _number(data.get("clear_stepover"))

            database.tools.append(tool)

        if not database.tools:
            raise ToolDbError(f"{resolved.name} contains no usable tools.")
        return database
    finally:
        reader.close()


def _first_value(reader: SqliteReader, table: str, keys: Sequence[str]) -> str:
    if not reader.has_table(table):
        return ""
    for row in reader.rows(table):
        for key in keys:
            value = _text(row.get(key)).strip()
            if value:
                return value
    return ""


def estimate_min_clearance(
    geometry: Any, max_parts: int = 600, search_radius: float = 2.0
) -> float | None:
    if geometry is None or getattr(geometry, "is_empty", True):
        return None

    parts = list(getattr(geometry, "geoms", [geometry]))
    parts = [p for p in parts if p is not None and not p.is_empty]
    if len(parts) < 2 or len(parts) > max_parts:
        return None

    try:
        from shapely import STRtree
    except Exception:
        return None

    tree = STRtree(parts)
    best: float | None = None
    for index, part in enumerate(parts):
        try:
            neighbours = tree.query(part.buffer(search_radius))
        except Exception:
            return best
        for other in neighbours:
            other = int(other)
            if other <= index:
                continue
            distance = float(part.distance(parts[other]))
            if distance <= 1e-9:
                continue
            if best is None or distance < best:
                best = distance
    return best


def _closest(tools: Sequence[DbTool], target: float) -> DbTool | None:
    if not tools:
        return None
    return min(tools, key=lambda t: abs(t.diameter - target))


def _closest_angle(tools: Sequence[DbTool], target: float) -> DbTool | None:
    if not tools:
        return None
    return min(tools, key=lambda t: (abs(t.angle - target), t.tip_diameter))


def _fit_within(
    tools: Sequence[DbTool], limit: float, depth: float
) -> DbTool | None:
    fitting = [t for t in tools if t.cut_width(depth) <= limit + 1e-9]
    if not fitting:
        return None
    return max(fitting, key=lambda t: t.cut_width(depth))


def recommend_tools(
    database: ToolDatabase,
    *,
    isolation_depth: float = 0.05,
    min_gap: float | None = None,
) -> dict[str, Any]:
    flats = database.flats()
    vbits = database.vbits()
    conical = [t for t in vbits if t.cut_width(isolation_depth) > 0.0]

    roles: dict[str, DbTool | None] = {}
    reasons: dict[str, str] = {}

    if not conical and not flats:
        raise ToolDbError("Tool database has no milling tools to choose from.")

    if min_gap is None:
        isolation = _closest_angle(vbits, 30.0) or _closest(flats, 0.2)
        reasons["isolation"] = (
            "Default 30 deg V-bit (board clearance unknown)."
            if vbits
            else "Default 0.2 mm end mill (board clearance unknown)."
        )
    else:
        limit = max(min_gap * 0.9, 0.01)
        flat_fit = _fit_within(flats, limit, isolation_depth)
        if flat_fit is not None:
            isolation = flat_fit
            reasons["isolation"] = (
                f"{flat_fit.diameter:.3f} mm end mill fits the "
                f"{min_gap:.3f} mm copper clearance."
            )
        else:
            cone_fit = _fit_within(conical, limit, isolation_depth)
            if cone_fit is not None:
                isolation = cone_fit
                reasons["isolation"] = (
                    f"{cone_fit.angle:.0f} deg V-bit cuts "
                    f"{cone_fit.cut_width(isolation_depth):.3f} mm at "
                    f"{isolation_depth:.3f} mm depth, inside the "
                    f"{min_gap:.3f} mm clearance."
                )
            else:
                pool = conical or vbits or flats
                isolation = min(pool, key=lambda t: t.cut_width(isolation_depth))
                reasons["isolation"] = (
                    f"Tightest available tool; the {min_gap:.3f} mm copper "
                    f"clearance is smaller than every tool's cut width."
                )
    roles["isolation"] = isolation

    if min_gap is None:
        roles["rubout"] = _closest(flats, 0.8)
        reasons["rubout"] = "Default 0.8 mm end mill."
    else:
        limit = max(min(min_gap * 0.9, 1.0), 0.05)
        roles["rubout"] = _fit_within(flats, limit, 0.0) or _closest(flats, 0.8)
        chosen = roles["rubout"]
        reasons["rubout"] = (
            f"{chosen.diameter:.3f} mm end mill clears copper without "
            f"touching the traces."
            if chosen
            else "No suitable rub-out tool."
        )

    roles["cutout"] = _closest(flats, 1.0)
    chosen = roles["cutout"]
    reasons["cutout"] = (
        f"{chosen.diameter:.3f} mm end mill for the board outline."
        if chosen
        else "No suitable cut-out tool."
    )

    roles["silkscreen"] = _closest(flats, 0.3) or _closest_angle(vbits, 30.0)
    chosen = roles["silkscreen"]
    reasons["silkscreen"] = (
        f"{chosen.diameter:.3f} mm end mill for silkscreen cut-out."
        if chosen and chosen.kind == "flat"
        else "No suitable silkscreen tool."
    )

    return {
        "roles": {role: tool.to_dict() if tool else None for role, tool in roles.items()},
        "keys": {role: tool.key if tool else None for role, tool in roles.items()},
        "reasons": reasons,
        "min_gap": round(min_gap, 4) if min_gap is not None else None,
        "isolation_depth": isolation_depth,
    }


def apply_role(
    config: Any,
    role: str,
    tool: DbTool | ToolSpec | dict[str, Any] | None,
) -> None:
    if tool is None:
        return
    if isinstance(tool, dict):
        spec = ToolSpec(
            name=str(tool.get("name", "")),
            kind=str(tool.get("kind", "flat")),
            diameter=float(tool.get("diameter", 0.0) or 0.0),
            tip_diameter=float(tool.get("tip_diameter", 0.0) or 0.0),
            angle=float(tool.get("angle", 0.0) or 0.0),
            feed_rate=float(tool.get("feed_rate", 0.0) or 0.0),
            plunge_rate=float(tool.get("plunge_rate", 0.0) or 0.0),
            spindle_rpm=int(tool.get("spindle_speed", 0) or 0),
            stepdown=float(tool.get("stepdown", 0.0) or 0.0),
            stepover=float(tool.get("stepover", 0.0) or 0.0),
            source=str(tool.get("key", "") or ""),
        )
    elif isinstance(tool, DbTool):
        spec = tool.to_tool_spec()
    else:
        spec = tool

    attribute = {
        "isolation": "isolation_tool",
        "rubout": "rubout_tool",
        "cutout": "cutout_tool",
        "silkscreen": "silkscreen_tool",
    }.get(role)
    if attribute is None or not hasattr(config, attribute):
        return
    setattr(config, attribute, spec)
