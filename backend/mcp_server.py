
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import platform
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exporter import write_package
from gerber_io import LayerType, PcbProject, load_files, load_project
from pcb_engine import SlicerConfig, ToolSpec, plan_toolpaths
from tool_db import (
    ToolDatabase,
    ToolDbError,
    apply_role,
    estimate_min_clearance,
    find_tool_db,
    load_tool_db,
    recommend_tools,
)
from updater import check_for_update, parse_version
from wegstr_gcode import MachineProfile, emit_plan, estimate_runtime

try:
    from main import VERSION
except Exception:
    VERSION = "1.1.5"
SERVER_NAME = "wegstr-pcb-slicer"
PROTOCOL_VERSION = "2025-06-18"

_APP_MODULES = (
    "main",
    "gerber_io",
    "pcb_engine",
    "wegstr_gcode",
    "exporter",
    "gcode_verify",
    "gcode_reader",
    "tool_db",
    "sqlite_read",
    "updater",
    "sample_gerber",
)

SKILL_TEXT = """# Wegstr PCB Slicer

Use the `wegstr-pcb-slicer` MCP server to turn PCB artwork into G-code for a
Wegstr Light CNC (140 x 90 x 40 mm).

## Start here

1. `app_info` - version, paths, whether a board is loaded.
2. `list_tools` - the Vectric tool database, grouped, with cutting data.
3. Load artwork: `open_archive` for a Gerber .zip, or `add_files` for
   individual .gtl/.gbl/.gko/.drl files.
4. `list_layers` - check the role assigned to each file before planning.
5. `recommend_tools` - measures the copper clearance and picks cutters.
6. `plan` - compute toolpaths and see the operation list and runtime.
7. `export` - write numbered G-code files plus a README build guide.

## Rules that matter

- Always `list_layers` after loading. If a file got the wrong role, reload it
  with `add_files` and pass `roles`.
- Never assume a board fits. `plan` reports the envelope; a board larger than
  140 x 90 mm is a hard error.
- Isolation depth is normally 0.05 mm. Cut-out depth must exceed the board
  thickness (1.6 mm) to cut through.
- Read `plan.warnings`. Flip instructions for double-sided boards appear there.

## Full access

`python` runs arbitrary Python inside the app with every module imported and
the live session object bound to `state`. Use it to inspect anything, prototype
a new feature, or compute something no tool exposes. `describe_module` lists a
module's classes and function signatures first.

`read_file`, `write_file` and `list_dir` reach the whole filesystem.

## Config

`get_config` / `set_config` change the live slicer settings. Fields include
`isolation_tool`, `cutout_tool`, `isolation_depth`, `isolation_passes`,
`cutout_depth`, `mill_bottom`, `silkscreen_enabled`, `alignment_holes`.
"""


@dataclass
class State:

    project: PcbProject | None = None
    config: SlicerConfig = field(default_factory=SlicerConfig)
    profile: MachineProfile = field(default_factory=MachineProfile)
    plan: Any = None
    tool_db: ToolDatabase | None = None
    tool_db_error: str | None = None
    source: str | None = None
    last_export: dict[str, Any] | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "project": str(self.source) if self.source else None,
            "layers": len(self.project.layers) if self.project else 0,
            "drills": len(self.project.drills) if self.project else 0,
            "holes": len(self.project.all_holes) if self.project else 0,
            "planned": self.plan is not None,
            "tool_db": str(self.tool_db.path) if self.tool_db else None,
        }

    def tool_database(self) -> ToolDatabase:
        if self.tool_db is None:
            try:
                self.tool_db = load_tool_db()
            except ToolDbError as exc:
                self.tool_db_error = str(exc)
                raise
        return self.tool_db


STATE = State()


class ToolError(Exception):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


def _layer_rows(project: PcbProject) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for layer in project.layers:
        bounds = layer.bounds
        rows.append(
            {
                "file": layer.name,
                "role": layer.layer_type.value,
                "display": layer.layer_type.display_name,
                "confidence": round(layer.confidence, 3),
                "reason": layer.reason,
                "area": round(layer.geometry.area, 3) if layer.geometry is not None else 0.0,
                "bounds": [round(v, 3) for v in bounds] if bounds else None,
            }
        )
    return rows


def _drill_rows(project: PcbProject) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for drill in project.drills:
        rows.append(
            {
                "file": drill.name,
                "role": drill.layer_type.value,
                "holes": len(drill.holes),
                "diameters": {
                    f"{d:.3f}": len(h) for d, h in drill.by_diameter().items()
                },
            }
        )
    return rows


def _require_project() -> PcbProject:
    if STATE.project is None:
        raise ToolError("No board loaded. Call open_archive or add_files first.")
    return STATE.project


def _tool(obj: ToolSpec | None) -> dict[str, Any] | None:
    return obj.to_dict() if obj is not None else None


def _config_dict() -> dict[str, Any]:
    from dataclasses import fields

    result: dict[str, Any] = {}
    for info in fields(SlicerConfig):
        value = getattr(STATE.config, info.name)
        result[info.name] = value.to_dict() if isinstance(value, ToolSpec) else value
    return result


def _apply_config(patch: dict[str, Any]) -> dict[str, Any]:
    applied: list[str] = []
    for key, value in patch.items():
        if not hasattr(STATE.config, key):
            continue
        current = getattr(STATE.config, key)
        if isinstance(current, ToolSpec) and isinstance(value, dict):
            setattr(STATE.config, key, ToolSpec(**{**current.to_dict(), **value}))
        elif isinstance(current, bool):
            setattr(STATE.config, key, bool(value))
        elif isinstance(current, int) and not isinstance(current, bool):
            setattr(STATE.config, key, int(value))
        elif isinstance(current, float):
            setattr(STATE.config, key, float(value))
        else:
            setattr(STATE.config, key, value)
        applied.append(key)
    STATE.plan = None
    return {"applied": applied, "config": _config_dict()}


def _do_open_archive(path: str) -> dict[str, Any]:
    resolved = Path(path).expanduser()
    if STATE.project is not None:
        STATE.project.cleanup()
        STATE.project = None
    STATE.project = load_project(resolved)
    STATE.source = str(resolved)
    STATE.plan = None
    return _project_payload()


def _do_add_files(paths: list[str], roles: dict[str, str] | None = None) -> dict[str, Any]:
    resolved = [Path(p).expanduser() for p in paths]
    if STATE.project is not None:
        STATE.project.cleanup()
    STATE.project = load_files(resolved, roles=roles)
    STATE.source = ", ".join(str(p) for p in resolved[:4]) + (
        " ..." if len(resolved) > 4 else ""
    )
    STATE.plan = None
    return _project_payload()


def _project_payload() -> dict[str, Any]:
    project = _require_project()
    payload: dict[str, Any] = {
        "source": str(project.source),
        "layers": _layer_rows(project),
        "drills": _drill_rows(project),
        "warnings": project.warnings,
    }
    outline = project.outline
    if outline is not None and outline.bounds is not None:
        minx, miny, maxx, maxy = outline.bounds
        payload["artwork"] = {
            "width": round(maxx - minx, 3),
            "height": round(maxy - miny, 3),
            "bounds": [round(v, 3) for v in outline.bounds],
        }
        fits = (maxx - minx) <= STATE.profile.work_x and (maxy - miny) <= STATE.profile.work_y
        payload["fits_work_area"] = bool(fits)
        if not fits:
            payload["note"] = (
                f"Artwork is {maxx - minx:.1f} x {maxy - miny:.1f} mm but the "
                f"machine bed is {STATE.profile.work_x:.0f} x {STATE.profile.work_y:.0f} mm."
            )
    else:
        payload["note"] = "No board outline layer was found."
    return payload


def _do_plan(include_polylines: bool = False) -> dict[str, Any]:
    project = _require_project()
    STATE.plan = plan_toolpaths(project, STATE.config)
    plan = STATE.plan
    groups: list[dict[str, Any]] = []
    for group in plan.groups:
        row = {
            "name": group.name,
            "kind": group.kind,
            "side": group.side,
            "tool": group.tool.name,
            "paths": len(group.polylines),
            "holes": len(group.holes),
            "cut_length": round(group.cut_length, 2),
            "passes": group.depth.passes(),
            "flip_before": group.requires_flip_before,
        }
        if include_polylines:
            row["polylines"] = [
                {
                    "closed": poly.closed,
                    "points": [[round(float(x), 3), round(float(y), 3)] for x, y in poly.points],
                }
                for poly in group.polylines
            ]
        groups.append(row)
    return {
        "board": {
            "width": round(plan.board.width, 3),
            "height": round(plan.board.height, 3),
            "holes": len(plan.board.holes),
        },
        "groups": groups,
        "total_cut_length": round(plan.total_cut_length, 2),
        "estimated_runtime": estimate_runtime(plan, STATE.profile, STATE.config),
        "warnings": plan.warnings,
    }


def _do_export(out_dir: str, stem: str | None = None) -> dict[str, Any]:
    if STATE.plan is None:
        _do_plan()
    plan = STATE.plan
    target = Path(out_dir).expanduser()
    package = write_package(
        plan,
        STATE.config,
        STATE.profile,
        target,
        stem or "board",
    )
    STATE.last_export = {
        "directory": str(package.directory),
        "files": [f.filename for f in package.files],
    }
    report = package.verification
    return {
        "directory": str(package.directory),
        "files": [
            {
                "file": f.filename,
                "minutes": round(f.estimated_minutes, 2),
                "tools": f.tools,
                "flip_before": f.requires_flip_before,
            }
            for f in package.files
        ],
        "readme": str(package.readme) if package.readme else None,
        "verification": report.to_dict() if report is not None else None,
    }


def _do_list_tools() -> dict[str, Any]:
    database = STATE.tool_database()
    return {
        "path": str(database.path),
        "machine": database.machine,
        "material": database.material,
        "groups": [
            {"group": name, "tools": [t.to_dict() for t in tools]}
            for name, tools in database.groups().items()
        ],
    }


def _do_recommend() -> dict[str, Any]:
    database = STATE.tool_database()
    gap = None
    if STATE.project is not None:
        for layer in STATE.project.layers:
            if layer.layer_type.is_copper and layer.geometry is not None:
                gap = estimate_min_clearance(layer.geometry)
                break
    result = recommend_tools(
        database, isolation_depth=STATE.config.isolation_depth, min_gap=gap
    )
    return {
        "copper_clearance": round(gap, 4) if gap is not None else None,
        "recommended": result["keys"],
        "reasons": result["reasons"],
        "roles": {role: (tool["name"] if tool else None) for role, tool in result["roles"].items()},
    }


def _do_select_tool(role: str, key: str | None = None) -> dict[str, Any]:
    database = STATE.tool_database()
    tool = database.by_key(key) if key else None
    if key and tool is None:
        raise ToolError(f"No tool with key {key!r}")
    if tool is None:
        gap = None
        if STATE.project is not None:
            for layer in STATE.project.layers:
                if layer.layer_type.is_copper and layer.geometry is not None:
                    gap = estimate_min_clearance(layer.geometry)
                    break
        result = recommend_tools(
            database, isolation_depth=STATE.config.isolation_depth, min_gap=gap
        )
        tool = database.by_key(result["keys"].get(role))
    apply_role(STATE.config, role, tool)
    STATE.plan = None
    return {
        "role": role,
        "tool": tool.to_dict() if tool else None,
        "config": _config_dict(),
    }


_EVAL_NAMESPACE: dict[str, Any] | None = None


def _sandbox() -> dict[str, Any]:
    global _EVAL_NAMESPACE
    if _EVAL_NAMESPACE is None:
        namespace: dict[str, Any] = {"__name__": "__wegstr_mcp__"}
        for name in _APP_MODULES:
            try:
                namespace[name] = __import__(name)
            except Exception:
                pass
        namespace["state"] = STATE
        namespace["Path"] = Path
        _EVAL_NAMESPACE = namespace
    _EVAL_NAMESPACE["state"] = STATE
    return _EVAL_NAMESPACE


def _do_python(code: str, timeout: float | None = None) -> dict[str, Any]:
    del timeout
    namespace = _sandbox()
    buffer = io.StringIO()
    result: Any = None
    error: str | None = None
    try:
        tree = ast.parse(code, mode="exec")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body.pop().value)
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            if tree.body:
                exec(compile(tree, "<mcp>", "exec"), namespace)
            if last is not None:
                result = eval(compile(last, "<mcp>", "eval"), namespace)
    except Exception:
        error = traceback.format_exc()
    payload: dict[str, Any] = {"stdout": buffer.getvalue()}
    if result is not None:
        payload["result"] = repr(result)
    if error:
        payload["error"] = error
    return payload


def _do_describe(name: str) -> dict[str, Any]:
    module = _sandbox().get(name)
    if module is None:
        try:
            module = __import__(name)
            _sandbox()[name] = module
        except Exception as exc:
            raise ToolError(f"Cannot import {name!r}: {exc}") from exc
    import inspect

    members: list[dict[str, Any]] = []
    for attr_name, value in sorted(vars(module).items()):
        if attr_name.startswith("_"):
            continue
        if not (inspect.isfunction(value) or inspect.isclass(value)):
            continue
        owner = getattr(value, "__module__", "") or ""
        if owner.startswith("typing") or owner in ("builtins", "abc"):
            continue
        try:
            signature = str(inspect.signature(value))
        except (TypeError, ValueError):
            signature = "(...)"
        members.append(
            {
                "name": attr_name,
                "kind": "class" if inspect.isclass(value) else "function",
                "signature": signature,
                "defined_in": owner,
                "summary": (inspect.getdoc(value) or "").split("\n")[0],
            }
        )
    return {"module": name, "members": members}


def _do_read_file(path: str, max_bytes: int = 400000) -> dict[str, Any]:
    resolved = Path(path).expanduser()
    data = resolved.read_bytes()[:max_bytes]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return {"path": str(resolved), "bytes": resolved.stat().st_size, "text": text}


def _do_write_file(path: str, content: str) -> dict[str, Any]:
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return {"path": str(resolved), "bytes": resolved.stat().st_size}


def _do_list_dir(path: str) -> dict[str, Any]:
    resolved = Path(path).expanduser()
    entries = []
    for item in sorted(resolved.iterdir()):
        entries.append(
            {
                "name": item.name,
                "dir": item.is_dir(),
                "bytes": item.stat().st_size if item.is_file() else None,
            }
        )
    return {"path": str(resolved), "entries": entries}


TOOLS: dict[str, dict[str, Any]] = {
    "app_info": {
        "description": "Version, paths, loaded board and machine profile.",
        "schema": {"type": "object", "properties": {}},
        "handler": lambda args: {
            "version": VERSION,
            "server": SERVER_NAME,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable,
            "backend": str(Path(__file__).resolve().parent),
            "tool_db": str(STATE.tool_db.path) if STATE.tool_db else str(find_tool_db() or ""),
            "tool_db_error": STATE.tool_db_error,
            "machine": STATE.profile.to_dict(),
            "state": STATE.describe(),
        },
    },
    "open_archive": {
        "description": "Load a Gerber .zip archive or a folder of Gerber/Excellon files.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "handler": lambda args: _do_open_archive(str(args["path"])),
    },
    "add_files": {
        "description": (
            "Load individual Gerber and drill files, one by one. Optionally map "
            "file names to roles (edge_cuts, top_copper, bottom_copper, top_silk, "
            "bottom_silk, top_mask, bottom_mask, drill_pth, drill_npth)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "roles": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["paths"],
        },
        "handler": lambda args: _do_add_files(
            [str(p) for p in args["paths"]], args.get("roles")
        ),
    },
    "list_layers": {
        "description": "Show every loaded layer, its role, area and bounds.",
        "schema": {"type": "object", "properties": {}},
        "handler": lambda args: _project_payload(),
    },
    "get_config": {
        "description": "Return the live slicer configuration.",
        "schema": {"type": "object", "properties": {}},
        "handler": lambda args: _config_dict(),
    },
    "set_config": {
        "description": "Update slicer configuration fields.",
        "schema": {
            "type": "object",
            "properties": {"config": {"type": "object"}},
            "required": ["config"],
        },
        "handler": lambda args: _apply_config(args["config"]),
    },
    "list_tools": {
        "description": "List the Vectric tool database, grouped, with cutting data.",
        "schema": {"type": "object", "properties": {}},
        "handler": lambda args: _do_list_tools(),
    },
    "recommend_tools": {
        "description": "Measure copper clearance and recommend cutters per role.",
        "schema": {"type": "object", "properties": {}},
        "handler": lambda args: _do_recommend(),
    },
    "select_tool": {
        "description": "Apply a database tool to a role (isolation, rubout, cutout, silkscreen).",
        "schema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": ["isolation", "rubout", "cutout", "silkscreen"],
                },
                "key": {"type": "string"},
            },
            "required": ["role"],
        },
        "handler": lambda args: _do_select_tool(str(args["role"]), args.get("key")),
    },
    "plan": {
        "description": "Compute toolpaths. Returns the operation list and runtime.",
        "schema": {
            "type": "object",
            "properties": {"include_polylines": {"type": "boolean"}},
        },
        "handler": lambda args: _do_plan(bool(args.get("include_polylines", False))),
    },
    "export": {
        "description": "Write numbered G-code files and a README build guide.",
        "schema": {
            "type": "object",
            "properties": {
                "out_dir": {"type": "string"},
                "stem": {"type": "string"},
            },
            "required": ["out_dir"],
        },
        "handler": lambda args: _do_export(str(args["out_dir"]), args.get("stem")),
    },
    "python": {
        "description": (
            "Run arbitrary Python inside the app. Every module is imported and the "
            "live session is bound to `state`. Use this to inspect or extend the app."
        ),
        "schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        "handler": lambda args: _do_python(str(args["code"])),
    },
    "describe_module": {
        "description": "List a module's public classes and functions with signatures.",
        "schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "handler": lambda args: _do_describe(str(args["name"])),
    },
    "read_file": {
        "description": "Read any text file.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "handler": lambda args: _do_read_file(str(args["path"])),
    },
    "write_file": {
        "description": "Write any text file.",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        "handler": lambda args: _do_write_file(str(args["path"]), str(args["content"])),
    },
    "list_dir": {
        "description": "List a directory.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "handler": lambda args: _do_list_dir(str(args["path"])),
    },
    "check_update": {
        "description": "Ask GitHub whether a newer release exists.",
        "schema": {"type": "object", "properties": {}},
        "handler": lambda args: {
            "current": VERSION,
            "update": (
                None
                if (info := check_for_update(current=VERSION)) is None
                else {
                    "version": info.version,
                    "url": info.url,
                    "prerelease": info.prerelease,
                }
            ),
        },
    },
    "parse_version": {
        "description": "Compare two version strings; returns -1, 0 or 1.",
        "schema": {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a", "b"],
        },
        "handler": lambda args: {
            "a": parse_version(str(args["a"])),
            "b": parse_version(str(args["b"])),
            "cmp": (parse_version(str(args["a"])) > parse_version(str(args["b"])))
            - (parse_version(str(args["a"])) < parse_version(str(args["b"]))),
        },
    },
}


def _resource_list() -> list[dict[str, Any]]:
    return [
        {"uri": "wegstr://app", "name": "Application state", "mimeType": "application/json"},
        {"uri": "wegstr://project", "name": "Loaded board", "mimeType": "application/json"},
        {"uri": "wegstr://config", "name": "Slicer configuration", "mimeType": "application/json"},
        {"uri": "wegstr://plan", "name": "Toolpath plan", "mimeType": "application/json"},
        {"uri": "wegstr://tools", "name": "Tool database", "mimeType": "application/json"},
        {"uri": "wegstr://skill", "name": "Usage skill", "mimeType": "text/markdown"},
        {"uri": "wegstr://source/{module}", "name": "Module source", "mimeType": "text/x-python"},
    ]


def _read_resource(uri: str) -> dict[str, Any]:
    if uri == "wegstr://app":
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": _json(
                {
                    "version": VERSION,
                    "executable": sys.executable,
                    "backend": str(Path(__file__).resolve().parent),
                    "state": STATE.describe(),
                }
            ),
        }
    if uri == "wegstr://project":
        return {"uri": uri, "mimeType": "application/json", "text": _json(_project_payload())}
    if uri == "wegstr://config":
        return {"uri": uri, "mimeType": "application/json", "text": _json(_config_dict())}
    if uri == "wegstr://plan":
        if STATE.plan is None:
            raise ToolError("No plan yet. Call the plan tool first.")
        return {"uri": uri, "mimeType": "application/json", "text": _json(_do_plan(False))}
    if uri == "wegstr://tools":
        return {"uri": uri, "mimeType": "application/json", "text": _json(_do_list_tools())}
    if uri == "wegstr://skill":
        return {"uri": uri, "mimeType": "text/markdown", "text": SKILL_TEXT}
    if uri.startswith("wegstr://source/"):
        name = uri[len("wegstr://source/"):]
        module = _sandbox().get(name)
        if module is None:
            try:
                module = __import__(name)
            except Exception as exc:
                raise ToolError(f"Cannot import {name!r}: {exc}") from exc
        source = getattr(module, "__file__", "") or ""
        if not source:
            raise ToolError(f"No source file for {name!r}")
        return {
            "uri": uri,
            "mimeType": "text/x-python",
            "text": Path(source).read_text(encoding="utf-8", errors="replace"),
        }
    raise ToolError(f"Unknown resource {uri!r}")


def _prompt_list() -> list[dict[str, Any]]:
    return [
        {
            "name": "wegstr-build-board",
            "description": "Mill a PCB from Gerber artwork end to end.",
            "arguments": [{"name": "path", "description": "Gerber .zip path", "required": False}],
        },
        {
            "name": "wegstr-extend-app",
            "description": "Add a feature the app does not have yet.",
            "arguments": [{"name": "feature", "description": "What to add", "required": False}],
        },
        {
            "name": "wegstr-inspect",
            "description": "Inspect the live session and report state.",
            "arguments": [],
        },
    ]


def _prompt_get(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    path = arguments.get("path", "")
    feature = arguments.get("feature", "")
    if name == "wegstr-build-board":
        text = (
            "Mill a PCB on the Wegstr Light CNC using the wegstr-pcb-slicer MCP server.\n\n"
            + (f"Artwork: {path}\n\n" if path else "")
            + "Steps: app_info, then open_archive (or add_files for individual "
            "files), list_layers to confirm roles, recommend_tools, plan, review "
            "warnings, then export. Report the operation list, runtime and the "
            "export folder."
        )
    elif name == "wegstr-extend-app":
        text = (
            "Extend the Wegstr PCB Slicer with: "
            + (feature or "the feature described below")
            + "\n\nFirst call describe_module to learn the API, then use the "
            "python tool to prototype against the live session, then write the "
            "change to the source files with write_file. Keep the code free of "
            "comments and docstrings, and do not add dependencies."
        )
    elif name == "wegstr-inspect":
        text = (
            "Read wegstr://app, wegstr://project, wegstr://config and "
            "wegstr://plan and summarise the current session, flagging anything "
            "that would fail on the machine."
        )
    else:
        raise ToolError(f"Unknown prompt {name!r}")
    return {
        "description": name,
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if request_id is None:
        return None

    def result(value: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": value}

    def failure(code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    try:
        if method == "initialize":
            requested = params.get("protocolVersion") or PROTOCOL_VERSION
            return result(
                {
                    "protocolVersion": requested,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {"name": SERVER_NAME, "version": VERSION},
                    "instructions": SKILL_TEXT,
                }
            )

        if method == "ping":
            return result({})

        if method == "tools/list":
            return result(
                {
                    "tools": [
                        {
                            "name": name,
                            "description": spec["description"],
                            "inputSchema": spec["schema"],
                        }
                        for name, spec in TOOLS.items()
                    ]
                }
            )

        if method == "tools/call":
            name = str(params.get("name", ""))
            spec = TOOLS.get(name)
            if spec is None:
                return failure(-32601, f"Unknown tool {name!r}")
            try:
                payload = spec["handler"](params.get("arguments") or {})
                return result(
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": payload if isinstance(payload, str) else _json(payload),
                            }
                        ],
                        "isError": False,
                    }
                )
            except Exception as exc:
                return result(
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}",
                            }
                        ],
                        "isError": True,
                    }
                )

        if method == "resources/list":
            return result({"resources": _resource_list()})

        if method == "resources/read":
            return result({"contents": [_read_resource(str(params.get("uri", "")))]})

        if method == "prompts/list":
            return result({"prompts": _prompt_list()})

        if method == "prompts/get":
            return result(
                _prompt_get(str(params.get("name", "")), params.get("arguments") or {})
            )

        if method in (
            "notifications/initialized",
            "notifications/cancelled",
            "logging/setLevel",
        ):
            return result({})

        return failure(-32601, f"Method not found: {method}")
    except ToolError as exc:
        return failure(-32602, str(exc))
    except Exception as exc:
        return failure(-32603, f"{type(exc).__name__}: {exc}")


def serve(stdin=None, stdout=None) -> int:
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    for stream, kwargs in ((source, {"encoding": "utf-8"}), (sink, {"encoding": "utf-8"})):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(**kwargs)
            except (AttributeError, ValueError):
                pass

    for raw in source:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            sink.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }
                )
                + "\n"
            )
            sink.flush()
            continue

        if isinstance(request, list):
            replies = [handle(item) for item in request if isinstance(item, dict)]
            replies = [reply for reply in replies if reply is not None]
            if replies:
                sink.write(json.dumps(replies) + "\n")
                sink.flush()
            continue

        reply = handle(request)
        if reply is not None:
            sink.write(json.dumps(reply) + "\n")
            sink.flush()
    return 0


def claude_config() -> dict[str, Any]:
    if getattr(sys, "frozen", False):
        command = sys.executable
        args = ["--mcp"]
    else:
        command = sys.executable
        args = [str(Path(__file__).resolve())]
    return {
        "mcpServers": {
            "wegstr": {
                "command": command,
                "args": args,
            }
        }
    }


def setup_prompt() -> str:
    config = json.dumps(claude_config(), indent=2)
    return (
        "Add the Wegstr PCB Slicer MCP server to Claude Code, then use it to "
        "mill PCBs.\n\n"
        "Option A - one command:\n"
        "  claude mcp add wegstr -s user -- "
        + " ".join([claude_config()["mcpServers"]["wegstr"]["command"]]
                  + claude_config()["mcpServers"]["wegstr"]["args"])
        + "\n\n"
        "Option B - add this to .mcp.json in your project:\n"
        f"{config}\n\n"
        "Then ask Claude Code:\n"
        '  "Use the wegstr MCP server to inspect the app, then load my Gerber '
        'zip, check the layer roles, recommend tools, plan the job and export '
        'the G-code."\n\n'
        "The server exposes the whole app: loading archives or individual "
        "files, layer inspection, the tool database, planning, export, the "
        "filesystem, and a python tool that can run anything inside the app."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wegstr-mcp")
    parser.add_argument("--print-config", action="store_true")
    parser.add_argument("--print-prompt", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.print_config:
        print(_json(claude_config()))
        return 0
    if args.print_prompt:
        print(setup_prompt())
        return 0
    if args.self_test:
        print(_json(handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]))
        return 0
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
