
from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from exporter import build_programs, verify_plan, write_package
from gerber_io import GerberError, LayerType, PcbProject, load_project
from pcb_engine import (
    SlicerConfig,
    SlicerError,
    ToolSpec,
    build_board,
    plan_toolpaths,
)
from tool_db import (
    ToolDbError,
    ToolDatabase,
    apply_role,
    estimate_min_clearance,
    find_tool_db,
    load_tool_db,
    recommend_tools,
)
from wegstr_gcode import (
    GCodeError,
    MachineProfile,
    WEGSTR_LIGHT,
    emit_plan,
    estimate_runtime,
)

VERSION = "1.1.0"

DEFAULT_ARCHIVE = Path(
    r"C:\Users\charles\Downloads\Gerber_Turretv2_PCB_Turretv2_2026-09-15.zip"
)

TOOL_FIELDS = ("isolation_tool", "rubout_tool", "cutout_tool", "silkscreen_tool")

TOOL_ROLES = ("isolation", "rubout", "cutout", "silkscreen")


def _coerce(current: Any, value: Any) -> Any:
    if value is None:
        return current
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        return str(value)
    return value


def _apply_dict(instance: Any, data: dict[str, Any]) -> Any:
    for key, value in data.items():
        if not hasattr(instance, key):
            continue
        if key in TOOL_FIELDS and isinstance(value, dict):
            setattr(instance, key, tool_from_dict(value))
            continue
        setattr(instance, key, _coerce(getattr(instance, key), value))
    return instance


def tool_from_dict(data: dict[str, Any], base: ToolSpec | None = None) -> ToolSpec:
    return _apply_dict(base or ToolSpec(), data)


def config_from_dict(data: dict[str, Any] | None, base: SlicerConfig | None = None) -> SlicerConfig:
    return _apply_dict(base or SlicerConfig(), data or {})


def profile_from_dict(
    data: dict[str, Any] | None, base: MachineProfile | None = None
) -> MachineProfile:
    return _apply_dict(base or MachineProfile(), data or {})


def project_summary(project: PcbProject, profile: MachineProfile) -> dict[str, Any]:
    layers: list[dict[str, Any]] = []
    for layer in project.layers:
        bounds = layer.bounds
        layers.append(
            {
                "name": layer.name,
                "path": str(layer.path),
                "type": layer.layer_type.value,
                "display_name": layer.layer_type.display_name,
                "confidence": round(layer.confidence, 3),
                "reason": layer.reason,
                "area": round(layer.geometry.area, 4) if layer.geometry is not None else 0.0,
                "bounds": [round(v, 4) for v in bounds] if bounds else None,
                "warning_count": len(layer.warnings),
            }
        )

    drills: list[dict[str, Any]] = []
    for drill in project.drills:
        by_diameter = {
            f"{diameter:.3f}": len(holes) for diameter, holes in drill.by_diameter().items()
        }
        drills.append(
            {
                "name": drill.name,
                "path": str(drill.path),
                "type": drill.layer_type.value,
                "display_name": drill.layer_type.display_name,
                "hole_count": len(drill.holes),
                "tools": {str(k): round(v, 4) for k, v in drill.tools.items()},
                "holes_by_diameter": by_diameter,
            }
        )

    summary: dict[str, Any] = {
        "source": str(project.source),
        "layers": layers,
        "drills": drills,
        "warnings": project.warnings,
    }

    try:
        board = build_board(project, SlicerConfig(machine_x=profile.work_x, machine_y=profile.work_y))
        minx, miny, maxx, maxy = board.outline.bounds
        fits = board.width <= profile.work_x and board.height <= profile.work_y
        summary["board"] = {
            "width": round(board.width, 3),
            "height": round(board.height, 3),
            "artwork_width": round(board.source_size[0], 3),
            "artwork_height": round(board.source_size[1], 3),
            "bounds": [round(v, 3) for v in (minx, miny, maxx, maxy)],
            "outline": [
                [round(float(x), 4), round(float(y), 4)] for x, y in board.outline.exterior.coords
            ],
            "sides_available": sorted(board.copper.keys()),
            "hole_count": len(board.holes),
        }
        summary["fit"] = {
            "ok": bool(fits),
            "machine": [profile.work_x, profile.work_y, profile.work_z],
            "message": (
                f"Fits the {profile.work_x:.0f} x {profile.work_y:.0f} mm work area"
                if fits
                else (
                    f"Board is {board.width:.1f} x {board.height:.1f} mm and does not fit "
                    f"the {profile.work_x:.0f} x {profile.work_y:.0f} mm work area"
                )
            ),
        }
    except (SlicerError, GerberError) as exc:
        summary["board"] = None
        summary["fit"] = {"ok": False, "machine": [], "message": str(exc)}

    return summary


def plan_summary(plan, config: SlicerConfig, profile: MachineProfile) -> dict[str, Any]:
    return {
        "groups": [
            {
                "name": group.name,
                "kind": group.kind,
                "side": group.side,
                "tool": group.tool.to_dict(),
                "path_count": len(group.polylines),
                "hole_count": len(group.holes),
                "cut_length": round(group.cut_length, 2),
                "passes": group.depth.passes(),
                "requires_flip_before": group.requires_flip_before,
            }
            for group in plan.groups
        ],
        "total_cut_length": round(plan.total_cut_length, 2),
        "estimated_runtime": estimate_runtime(plan, profile, config),
        "warnings": plan.warnings,
    }


class Session:

    def __init__(self) -> None:
        self.profile = MachineProfile()
        self.config = SlicerConfig()
        self.project: PcbProject | None = None
        self.project_path: str | None = None
        self.tool_db_path: str | None = None
        self._tool_db: ToolDatabase | None = None

    def load(self, path: str) -> PcbProject:
        resolved = str(Path(path).expanduser())
        if self.project is not None and self.project_path == resolved:
            return self.project
        if self.project is not None:
            self.project.cleanup()
        self.project = load_project(resolved)
        self.project_path = resolved
        return self.project

    def require_project(self) -> PcbProject:
        if self.project is None:
            raise SlicerError("No project loaded. Send a 'load' request first.")
        return self.project

    def tool_db(self, path: str | None = None, reload: bool = False) -> ToolDatabase:
        if path:
            resolved = str(Path(path).expanduser())
            if reload or self._tool_db is None or self.tool_db_path != resolved:
                self._tool_db = load_tool_db(resolved)
                self.tool_db_path = resolved
            return self._tool_db
        if self._tool_db is None or reload:
            self._tool_db = load_tool_db()
            self.tool_db_path = str(self._tool_db.path) if self._tool_db.path else None
        return self._tool_db

    def close(self) -> None:
        if self.project is not None:
            self.project.cleanup()
            self.project = None
            self.project_path = None


def _load_from_params(session: Session, params: dict[str, Any]) -> PcbProject:
    path = params.get("path") or session.project_path or str(DEFAULT_ARCHIVE)
    return session.load(path)


def do_load(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    _load_from_params(session, params)
    return project_summary(session.require_project(), session.profile)


def do_plan(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    _load_from_params(session, params)
    session.config = config_from_dict(params.get("config"), session.config)
    session.profile = profile_from_dict(params.get("profile"), session.profile)

    project = session.require_project()
    plan = plan_toolpaths(project, session.config)
    payload = plan.to_dict()
    payload["summary"] = plan_summary(plan, session.config, session.profile)
    payload["estimated_runtime"] = estimate_runtime(plan, session.profile, session.config)
    return payload


def do_export(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    _load_from_params(session, params)
    session.config = config_from_dict(params.get("config"), session.config)
    session.profile = profile_from_dict(params.get("profile"), session.profile)

    project = session.require_project()
    plan = plan_toolpaths(project, session.config)

    out = params.get("out")
    if not out:
        stem = Path(session.project_path or "pcb").stem
        out = str(Path.cwd() / f"{stem}.nc")
    out_path = Path(out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    program = emit_plan(plan, session.config, session.profile, out_path.stem)
    out_path.write_text(program, encoding="utf-8", newline="\n")

    return {
        "path": str(out_path),
        "bytes": out_path.stat().st_size,
        "lines": program.count("\n"),
        "summary": plan_summary(plan, session.config, session.profile),
    }


def do_defaults(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for field_info in fields(SlicerConfig):
        value = getattr(session.config, field_info.name)
        config[field_info.name] = value.to_dict() if isinstance(value, ToolSpec) else value
    return {
        "version": VERSION,
        "config": config,
        "profile": session.profile.to_dict(),
        "tools": {
            "isolation": session.config.isolation_tool.to_dict(),
            "rubout": session.config.rubout_tool.to_dict(),
            "cutout": session.config.cutout_tool.to_dict(),
            "silkscreen": session.config.silkscreen_tool.to_dict(),
        },
        "tool_db": _tool_payload(session, {}),
        "default_archive": str(DEFAULT_ARCHIVE),
        "archive_exists": DEFAULT_ARCHIVE.exists(),
    }


def _min_gap(session: Session, params: dict[str, Any]) -> float | None:
    explicit = params.get("min_gap")
    if explicit is not None:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            return None
    project = session.project
    if project is None:
        return None
    for layer in project.layers:
        if layer.layer_type is LayerType.TOP_COPPER and layer.geometry is not None:
            return estimate_min_clearance(layer.geometry)
    for layer in project.layers:
        if layer.layer_type.is_copper and layer.geometry is not None:
            return estimate_min_clearance(layer.geometry)
    return None


def _tool_payload(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "available": False,
        "path": None,
        "found": None,
        "error": None,
    }
    try:
        database = session.tool_db(params.get("path"), bool(params.get("reload")))
    except ToolDbError as exc:
        found = find_tool_db()
        payload["found"] = str(found) if found else None
        payload["error"] = str(exc)
        return payload

    depth = params.get("isolation_depth")
    if depth is None:
        depth = session.config.isolation_depth
    recommendation = recommend_tools(
        database,
        isolation_depth=float(depth or 0.05),
        min_gap=_min_gap(session, params),
    )

    payload.update(database.to_dict())
    payload["available"] = True
    payload["found"] = str(database.path) if database.path else None
    payload["recommended"] = recommendation["keys"]
    payload["reasons"] = recommendation["reasons"]
    payload["min_gap"] = recommendation["min_gap"]
    payload["roles"] = recommendation["roles"]

    if params.get("apply"):
        for role, key in recommendation["keys"].items():
            apply_role(session.config, role, database.by_key(key))
        payload["applied"] = {
            role: getattr(session.config, f"{role}_tool").to_dict()
            for role in TOOL_ROLES
            if hasattr(session.config, f"{role}_tool")
        }
    return payload


def do_tools(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    if params.get("project"):
        session.load(str(params["project"]))
    return _tool_payload(session, params)


def do_select_tool(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    role = str(params.get("role", ""))
    if role not in TOOL_ROLES:
        raise SlicerError(f"Unknown tool role {role!r}. Known: {list(TOOL_ROLES)}")

    database = session.tool_db(params.get("path"), bool(params.get("reload")))
    key = params.get("key")
    tool = database.by_key(str(key)) if key else None
    if key and tool is None:
        raise SlicerError(f"Tool {key!r} is not in {database.path}")

    if tool is None:
        recommendation = recommend_tools(
            database,
            isolation_depth=float(
                params.get("isolation_depth") or session.config.isolation_depth
            ),
            min_gap=_min_gap(session, params),
        )
        tool = database.by_key(recommendation["keys"].get(role))

    apply_role(session.config, role, tool)
    attribute = f"{role}_tool"
    return {
        "role": role,
        "tool": tool.to_dict() if tool else None,
        "config": getattr(session.config, attribute).to_dict(),
    }


def do_validate(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    config = config_from_dict(params.get("config"), session.config)
    profile = profile_from_dict(params.get("profile"), session.profile)
    problems = config.validate() + profile.validate()
    return {"ok": not problems, "problems": problems}


def do_ping(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    return {"pong": True, "backend": "wegstr-slicer", "version": VERSION}


COMMANDS = {
    "ping": do_ping,
    "defaults": do_defaults,
    "tools": do_tools,
    "select_tool": do_select_tool,
    "load": do_load,
    "plan": do_plan,
    "export": do_export,
    "validate": do_validate,
}


def ipc_loop() -> int:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except (AttributeError, ValueError):
            pass

    session = Session()
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            request_id = None
            try:
                request = json.loads(raw)
                request_id = request.get("id")
                command = request.get("cmd")
                params = request.get("params") or {}
                handler = COMMANDS.get(command)
                if handler is None:
                    raise SlicerError(
                        f"Unknown command {command!r}. Known: {sorted(COMMANDS)}"
                    )
                result = handler(session, params)
                reply = {"id": request_id, "ok": True, "result": result}
            except Exception as exc:
                reply = {
                    "id": request_id,
                    "ok": False,
                    "error": str(exc),
                    "kind": type(exc).__name__,
                }
                if not isinstance(exc, (GerberError, SlicerError, GCodeError, ToolDbError)):
                    reply["traceback"] = traceback.format_exc()
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wegstr-slicer",
        description="Generate Wegstr Light CNC G-code from a Gerber archive.",
    )
    parser.add_argument(
        "--zip",
        "-z",
        default=str(DEFAULT_ARCHIVE),
        help="Gerber .zip archive or a folder containing one",
    )
    parser.add_argument("--out", "-o", default=None, help="Output .nc/.gcode path")
    parser.add_argument("--ipc", action="store_true", help="Run the JSON IPC server on stdio")
    parser.add_argument("--info", action="store_true", help="Print the layer report and exit")
    parser.add_argument("--preview", action="store_true", help="Print the toolpath plan as JSON")
    parser.add_argument("--emit-defaults", action="store_true", help="Print default config JSON")
    parser.add_argument("--config", default=None, help="JSON file or inline JSON of SlicerConfig")
    parser.add_argument("--profile", default=None, help="JSON file or inline JSON of MachineProfile")

    tool = parser.add_argument_group("tooling")
    tool.add_argument("--tool-diameter", type=float, help="Isolation tool diameter (mm)")
    tool.add_argument("--tool-angle", type=float, help="Isolation V-bit included angle (deg)")
    tool.add_argument("--tool-tip", type=float, help="Isolation V-bit tip flat diameter (mm)")
    tool.add_argument("--tool-kind", choices=["vbit", "flat"], help="Isolation tool type")
    tool.add_argument("--cutout-diameter", type=float, help="Cut-out tool diameter (mm)")
    tool.add_argument("--tool-db", default=None, help="Path to a Vectric .vtdb tool database")
    tool.add_argument(
        "--use-tool-db",
        action="store_true",
        help="Auto-select the isolation, rub-out, cut-out and silkscreen tools from the database",
    )
    tool.add_argument(
        "--list-tools",
        action="store_true",
        help="List the tools in the database, with recommendations, and exit",
    )

    iso = parser.add_argument_group("isolation")
    iso.add_argument("--passes", type=int, help="Number of isolation passes")
    iso.add_argument("--iso-depth", type=float, help="Isolation depth (mm)")
    iso.add_argument("--iso-stepover", type=float, help="Isolation stepover (mm, 0 = auto)")
    iso.add_argument("--iso-clearance", type=float, help="Extra clearance from copper (mm)")
    iso.add_argument("--no-isolation", action="store_true", help="Disable isolation milling")

    rub = parser.add_argument_group("rub-out")
    rub.add_argument("--rubout", action="store_true", help="Enable rub-out copper clearing")
    rub.add_argument("--rubout-depth", type=float, help="Rub-out depth (mm)")
    rub.add_argument("--rubout-stepover", type=float, help="Rub-out stepover (mm, 0 = auto)")

    cut = parser.add_argument_group("cut-out")
    cut.add_argument("--cut-depth", type=float, help="Board cut-out depth (mm)")
    cut.add_argument("--cut-stepdown", type=float, help="Cut-out stepdown (mm)")
    cut.add_argument("--tabs", type=int, help="Number of break-away tabs")
    cut.add_argument("--tab-width", type=float, help="Tab width (mm)")
    cut.add_argument("--no-tabs", action="store_true", help="Cut the outline in one go")
    cut.add_argument("--no-cutout", action="store_true", help="Skip the board cut-out")

    silk = parser.add_argument_group("silkscreen")
    silk.add_argument("--silkscreen", action="store_true", help="Engrave the silkscreen layer")
    silk.add_argument("--silkscreen-depth", type=float, help="Silkscreen engraving depth (mm)")
    silk.add_argument(
        "--silkscreen-over-pads",
        action="store_true",
        help="Do not clear the pad openings before engraving",
    )

    align = parser.add_argument_group("double-sided alignment")
    align.add_argument(
        "--alignment-holes",
        action="store_true",
        help="Drill two deep pin holes on the flip axis",
    )
    align.add_argument("--alignment-depth", type=float, help="Pin hole depth (mm)")
    align.add_argument("--alignment-diameter", type=float, help="Pin hole diameter (mm)")
    align.add_argument(
        "--flip-axis",
        choices=["horizontal", "vertical"],
        help="How the board is turned over for the second side",
    )

    drl = parser.add_argument_group("drilling")
    drl.add_argument("--no-drill", action="store_true", help="Skip drilling")
    drl.add_argument("--peck", type=float, help="Peck depth (mm)")
    drl.add_argument("--drill-extra", type=float, help="Drill depth past the board (mm)")

    setup = parser.add_argument_group("setup")
    setup.add_argument("--bottom", action="store_true", help="Also mill the bottom copper")
    setup.add_argument("--only-bottom", action="store_true", help="Mill the bottom copper only")
    setup.add_argument("--origin", choices=["lower_left", "center"], help="Work origin")
    setup.add_argument("--margin", type=float, help="Margin from the work origin (mm)")
    setup.add_argument("--feed", type=float, help="Cut feed rate (mm/min)")
    setup.add_argument("--plunge", type=float, help="Plunge feed rate (mm/min)")
    setup.add_argument("--rpm", type=int, help="Spindle speed (RPM)")
    setup.add_argument("--rapid-z", type=float, help="Rapid height (mm)")
    setup.add_argument("--line-numbers", action="store_true", help="Emit N-numbers")
    setup.add_argument("--allow-oversize", action="store_true", help="Skip the envelope check")

    out = parser.add_argument_group("output")
    out.add_argument(
        "--flat",
        action="store_true",
        help="Write one combined program instead of one file per operation",
    )
    out.add_argument(
        "--no-drill-first",
        action="store_true",
        help="Drill after the traces instead of before them",
    )
    out.add_argument(
        "--pins",
        type=int,
        help="Existing through-holes to document as double-sided alignment pins",
    )
    return parser


def _load_json_argument(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    candidate = Path(value)
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return json.loads(value)


def config_from_args(args: argparse.Namespace) -> tuple[SlicerConfig, MachineProfile]:
    config = config_from_dict(_load_json_argument(args.config))
    profile = profile_from_dict(_load_json_argument(args.profile))

    if args.tool_diameter is not None:
        config.isolation_tool.diameter = args.tool_diameter
    if args.tool_angle is not None:
        config.isolation_tool.angle = args.tool_angle
    if args.tool_tip is not None:
        config.isolation_tool.tip_diameter = args.tool_tip
    if args.tool_kind is not None:
        config.isolation_tool.kind = args.tool_kind
    if args.cutout_diameter is not None:
        config.cutout_tool.diameter = args.cutout_diameter

    if args.passes is not None:
        config.isolation_passes = args.passes
    if args.iso_depth is not None:
        config.isolation_depth = args.iso_depth
    if args.iso_stepover is not None:
        config.isolation_stepover = args.iso_stepover
    if args.iso_clearance is not None:
        config.isolation_clearance = args.iso_clearance
    if args.no_isolation:
        config.isolation_enabled = False

    if args.rubout:
        config.rubout_enabled = True
    if args.rubout_depth is not None:
        config.rubout_depth = args.rubout_depth
    if args.rubout_stepover is not None:
        config.rubout_stepover = args.rubout_stepover

    if args.cut_depth is not None:
        config.cutout_depth = args.cut_depth
    if args.cut_stepdown is not None:
        config.cutout_stepdown = args.cut_stepdown
    if args.tabs is not None:
        config.tab_count = args.tabs
    if args.tab_width is not None:
        config.tab_width = args.tab_width
    if args.no_tabs:
        config.tab_enabled = False
    if args.no_cutout:
        config.cutout_enabled = False

    if args.silkscreen:
        config.silkscreen_enabled = True
    if args.silkscreen_depth is not None:
        config.silkscreen_depth = args.silkscreen_depth
    if args.silkscreen_over_pads:
        config.silkscreen_clear_pads = False

    if args.alignment_holes:
        config.alignment_holes = True
    if args.alignment_depth is not None:
        config.alignment_hole_depth = args.alignment_depth
    if args.alignment_diameter is not None:
        config.alignment_hole_diameter = args.alignment_diameter
    if args.flip_axis is not None:
        config.flip_axis = args.flip_axis

    if args.no_drill:
        config.drill_enabled = False
    if args.peck is not None:
        config.peck_depth = args.peck
    if args.drill_extra is not None:
        config.drill_depth_extra = args.drill_extra

    if args.bottom:
        config.mill_bottom = True
    if args.only_bottom:
        config.mill_bottom = True
        config.mill_top = False
    if args.origin is not None:
        config.origin_mode = args.origin
    if args.margin is not None:
        config.margin = args.margin
    if args.allow_oversize:
        config.enforce_envelope = False

    if args.flat:
        config.split_output = False
    if args.no_drill_first:
        config.drill_first = False
    if args.pins is not None:
        config.registration_pins = args.pins

    config.machine_x = profile.work_x
    config.machine_y = profile.work_y
    config.machine_z = profile.work_z

    if args.feed is not None:
        profile.cut_feed = args.feed
    if args.plunge is not None:
        profile.plunge_feed = args.plunge
    if args.rpm is not None:
        profile.spindle_rpm = args.rpm
    if args.rapid_z is not None:
        profile.rapid_z = args.rapid_z
    if args.line_numbers:
        profile.line_numbers = True

    return config, profile


def print_info(summary: dict[str, Any]) -> None:
    print(f"Source : {summary['source']}")
    print()
    print("Layers")
    print(f"  {'file':<36} {'role':<14} {'area mm2':>10}  bounds")
    for layer in summary["layers"]:
        bounds = layer["bounds"]
        bound_text = (
            "(" + ", ".join(f"{v:.2f}" for v in bounds) + ")" if bounds else "-"
        )
        print(
            f"  {layer['name']:<36} {layer['display_name']:<14} "
            f"{layer['area']:>10.2f}  {bound_text}"
        )
    print()
    print("Drill programs")
    for drill in summary["drills"]:
        sizes = ", ".join(f"{d}mm x{n}" for d, n in drill["holes_by_diameter"].items())
        print(f"  {drill['name']:<36} {drill['hole_count']:>4} holes  [{sizes}]")
    board = summary.get("board")
    if board:
        print()
        print(
            f"Board  : {board['width']:.2f} x {board['height']:.2f} mm "
            f"(artwork {board['artwork_width']:.2f} x {board['artwork_height']:.2f} mm)"
        )
        print(f"Sides  : {', '.join(board['sides_available']) or 'none'}")
        print(f"Fit    : {summary['fit']['message']}")
    if summary["warnings"]:
        print()
        print(f"Warnings ({len(summary['warnings'])})")
        for warning in summary["warnings"][:20]:
            print(f"  ! {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.ipc:
        return ipc_loop()

    if args.emit_defaults:
        print(json.dumps(do_defaults(Session(), {}), indent=2))
        return 0

    try:
        if args.list_tools:
            session = Session()
            payload = _tool_payload(session, {"path": args.tool_db})
            if not payload.get("available"):
                print(f"error: {payload.get('error')}", file=sys.stderr)
                return 2
            print(f"Database : {payload['path']}")
            print(f"Machine  : {payload['machine']}")
            print(f"Material : {payload['material']}")
            print(f"Tools    : {payload['count']}")
            print()
            for group in payload["groups"]:
                print(f"[{group['name']}]")
                for tool in group["tools"]:
                    extra = (
                        f"{tool['angle']:.0f} deg" if tool["kind"] == "vbit"
                        else f"{tool['diameter']:.3f} mm"
                    )
                    print(
                        f"  {tool['name']:<26} {extra:<9} "
                        f"feed {tool['feed_rate']:.0f} plunge {tool['plunge_rate']:.0f} "
                        f"rpm {tool['spindle_speed']}"
                    )
                print()
            print("Recommended")
            for role, tool in payload["roles"].items():
                name = tool["name"] if tool else "-"
                print(f"  {role:<11} {name:<26} {payload['reasons'][role]}")
            return 0

        if args.info:
            session = Session()
            try:
                session.load(args.zip)
                print_info(project_summary(session.require_project(), session.profile))
            finally:
                session.close()
            return 0

        config, profile = config_from_args(args)

        if args.use_tool_db:
            session = Session()
            database = session.tool_db(args.tool_db)
            gap = None
            try:
                project = load_project(args.zip)
            except (GerberError, FileNotFoundError):
                project = None
            if project is not None:
                try:
                    for layer in project.layers:
                        if layer.layer_type is LayerType.TOP_COPPER and layer.geometry is not None:
                            gap = estimate_min_clearance(layer.geometry)
                            break
                finally:
                    project.cleanup()
            recommendation = recommend_tools(
                database, isolation_depth=config.isolation_depth, min_gap=gap
            )
            for role, key in recommendation["keys"].items():
                apply_role(config, role, database.by_key(key))
            if gap is not None:
                print(f"Measured copper clearance: {gap:.3f} mm")
            for role, key in recommendation["keys"].items():
                print(f"  {role:<11} {recommendation['reasons'][role]}")

        if args.preview:
            project = load_project(args.zip)
            try:
                plan = plan_toolpaths(project, config)
                payload = plan.to_dict()
                payload["summary"] = plan_summary(plan, config, profile)
                print(json.dumps(payload))
            finally:
                project.cleanup()
            return 0

        project = load_project(args.zip)
        try:
            plan = plan_toolpaths(project, config)
            stem = Path(args.zip).stem

            if config.split_output:
                out_dir = (
                    Path(args.out)
                    if args.out
                    else Path.cwd() / f"{stem}_milling"
                )
                if out_dir.suffix:
                    out_dir = out_dir.parent / out_dir.stem
                package = write_package(plan, config, profile, out_dir, stem)

                print(f"Wrote {len(package.files)} file(s) + README.md to {out_dir}")
                for export_file in package.files:
                    flip = "  [FLIP BOARD]" if export_file.requires_flip_before else ""
                    print(
                        f"  {export_file.filename:<24} "
                        f"{export_file.estimated_minutes:>6.1f} min  "
                        f"{', '.join(export_file.tools)}{flip}"
                    )
                report = package.verification
                if report is not None:
                    print()
                    print(f"Verification: {report.headline()}")
                    stats = report.stats
                    print(
                        f"  {stats.get('lines', 0)} lines, "
                        f"{stats.get('cut_distance', 0):.0f} mm of cutting"
                    )
                    bounds = stats.get("xy_bounds")
                    if bounds:
                        print(
                            f"  envelope X {bounds[0]:.2f}..{bounds[2]:.2f}  "
                            f"Y {bounds[1]:.2f}..{bounds[3]:.2f} mm"
                        )
                    for issue in report.issues[:10]:
                        print(f"  [{issue.severity.upper()}] {issue.message}")
                if report is not None and report.errors:
                    return 3
            else:
                out = (
                    Path(args.out)
                    if args.out
                    else Path.cwd() / f"{stem}.gcode"
                )
                out.parent.mkdir(parents=True, exist_ok=True)
                program = emit_plan(plan, config, profile, out.stem)
                out.write_text(program, encoding="utf-8", newline="\n")
                print(f"Wrote {out}")
                print(f"  {len(program.splitlines())} lines, {out.stat().st_size / 1024:.0f} KB")
                print(f"  estimated runtime {estimate_runtime(plan, profile, config)}")
                for group in plan.groups:
                    print(
                        f"  - {group.name:<32} {len(group.polylines):>5} paths "
                        f"{len(group.holes):>4} holes {group.cut_length:>9.1f} mm"
                    )
            for warning in plan.warnings:
                print(f"  ! {warning}")
        finally:
            project.cleanup()
        return 0

    except (GerberError, SlicerError, GCodeError, ToolDbError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
