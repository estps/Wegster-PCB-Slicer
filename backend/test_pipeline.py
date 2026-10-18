
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from exporter import verify_plan, write_package
from gcode_verify import verify_program
from gerber_io import LayerType, load_project
from pcb_engine import (
    SlicerConfig,
    SlicerError,
    ToolSpec,
    pin_axis,
    plan_toolpaths,
    reflect_about_pin_axis,
    select_registration_holes,
)
from tool_db import (
    ToolDbError,
    apply_role,
    estimate_min_clearance,
    find_tool_db,
    load_tool_db,
    recommend_tools,
)
from updater import parse_version
from wegstr_gcode import WEGSTR_LIGHT, emit_plan, estimate_runtime

DEFAULT_ARCHIVE = Path(
    r"C:\Users\charles\Downloads\Gerber_Turretv2_PCB_Turretv2_2026-09-15.zip"
)

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        FAILURES.append(message)


def test_gerber_parsing(archive: Path) -> None:
    print("\n== Gerber / Excellon parsing ==")
    project = load_project(archive)
    try:
        types = {layer.layer_type for layer in project.layers}
        check(LayerType.TOP_COPPER in types, "front copper layer detected")
        check(LayerType.BOTTOM_COPPER in types, "back copper layer detected")
        check(LayerType.EDGE_CUTS in types, "board outline detected")

        outline = project.outline
        check(outline is not None and outline.geometry is not None, "outline geometry parsed")
        if outline and outline.geometry is not None:
            check(outline.geometry.area > 0, "outline has positive area")

        copper = project.layer(LayerType.TOP_COPPER)
        check(
            copper is not None and copper.geometry is not None and copper.geometry.area > 0,
            "front copper has positive area",
        )

        check(len(project.drills) >= 1, f"found {len(project.drills)} drill programs")
        npth = [d for d in project.drills if d.layer_type is LayerType.DRILL_NPTH]
        check(len(npth) >= 1, "non-plated drill program recognised")
        check(
            all(not h.plated for d in npth for h in d.holes),
            "NPTH holes are marked unplated",
        )
        pth = [d for d in project.drills if d.layer_type is LayerType.DRILL_PTH]
        check(all(h.plated for d in pth for h in d.holes), "PTH holes are marked plated")
        check(
            all(h.diameter > 0 for h in project.all_holes),
            "every hole has a positive diameter",
        )
        check(
            not any(l.path.name.endswith(".txt") for l in project.layers),
            "readme .txt is not treated as a layer",
        )
    finally:
        project.cleanup()


def test_engine(archive: Path) -> None:
    print("\n== CAM engine ==")
    project = load_project(archive)
    try:
        config = SlicerConfig()
        config.isolation_passes = 2
        config.mill_bottom = True
        plan = plan_toolpaths(project, config)

        check(
            0 < plan.board.width <= config.machine_x,
            f"board width fits the bed ({plan.board.width:.2f} mm)",
        )
        check(
            0 < plan.board.height <= config.machine_y,
            f"board height fits the bed ({plan.board.height:.2f} mm)",
        )

        kinds = [g.kind for g in plan.groups]
        check("isolation" in kinds, "isolation group produced")
        check("drill" in kinds, "drill groups produced")
        check("cutout" in kinds, "cut-out group produced")
        check(
            sum(1 for k in kinds if k == "isolation") == 2,
            "isolation produced for both sides",
        )

        cutout = next(g for g in plan.groups if g.kind == "cutout")
        check(
            len(cutout.polylines) == config.tab_count,
            f"cut-out split into {len(cutout.polylines)} tabbed segments "
            f"(expected {config.tab_count})",
        )

        raw = sum(g.cut_length for g in plan.groups)
        passes = sum(max(1, len(g.depth.passes())) for g in plan.groups)
        check(
            abs(plan.total_cut_length - sum(
                g.cut_length * max(1, len(g.depth.passes())) for g in plan.groups
            )) < 1e-6,
            "total_cut_length accounts for depth passes",
        )
        check(
            len(cutout.depth.passes()) > 1,
            f"cut-out uses {len(cutout.depth.passes())} depth passes",
        )
        check(
            plan.total_cut_length > raw,
            f"total_cut_length ({plan.total_cut_length:.0f} mm) exceeds raw "
            f"geometry length ({raw:.0f} mm) across {passes} passes",
        )
        check(
            all(not p.closed for p in cutout.polylines),
            "tabbed cut-out paths are open",
        )

        radii = [
            ToolSpec("v", "vbit", 0.1, 0.0, 30.0).radius_at_depth(d) for d in (0.05, 0.15)
        ]
        check(radii[1] > radii[0], "V-bit cutting radius grows with depth")

        check(
            abs(ToolSpec("v", "vbit", 0.1, 0.0, 30.0).radius_at_depth(0.05) - 0.0134) < 0.001,
            "V-bit radius compensation at 0.05 mm depth",
        )

        oversize = SlicerConfig()
        oversize.machine_x = 10.0
        oversize.machine_y = 10.0
        try:
            plan_toolpaths(project, oversize)
            check(False, "oversize board rejected")
        except SlicerError:
            check(True, "oversize board rejected")

        bad = SlicerConfig()
        bad.isolation_passes = 0
        check(bool(bad.validate()), "invalid config is reported by validate()")
    finally:
        project.cleanup()


def test_gcode(archive: Path) -> None:
    print("\n== G-code generation ==")
    project = load_project(archive)
    try:
        config = SlicerConfig()
        plan = plan_toolpaths(project, config)
        program = emit_plan(plan, config, WEGSTR_LIGHT, "test")

        lines = program.splitlines()
        check(lines[0] == "%" and lines[-1] == "%", "program is delimited by %")
        check("G21" in lines, "metric units selected (G21)")
        check("G90" in lines, "absolute positioning selected (G90)")
        check(any(l.startswith("M3") for l in lines), "spindle starts with M3")
        check("M5" in lines, "spindle stops with M5")
        check(lines[-2] == "M30" or "M30" in lines, "program ends with M30")
        check(any(l.startswith("G4 P") for l in lines), "spindle spin-up dwell emitted")

        coords = [
            (float(m.group(1)), float(m.group(2)))
            for m in (re.match(r"^X(-?[\d.]+) Y(-?[\d.]+)", l) for l in lines)
            if m
        ]
        check(bool(coords), f"found {len(coords)} XY moves")
        check(
            all(0 <= x <= WEGSTR_LIGHT.work_x for x, _ in coords),
            "all X moves inside the 140 mm envelope",
        )
        check(
            all(0 <= y <= WEGSTR_LIGHT.work_y for _, y in coords),
            "all Y moves inside the 90 mm envelope",
        )

        z_values = [
            float(m.group(1))
            for m in (re.search(r"Z(-?[\d.]+)", l) for l in lines)
            if m
        ]
        check(min(z_values) >= -config.board_thickness - 1.0, "Z never exceeds cut depth")
        check(max(z_values) <= WEGSTR_LIGHT.tool_change_z, "Z stays under the ceiling")

        adjacent = [
            (lines[i - 1].split()[0], lines[i].split()[0])
            for i in range(1, len(lines))
            if lines[i - 1][:2] in ("G0", "G1") and lines[i][:2] in ("G0", "G1")
        ]
        duplicates = [(a, b) for a, b in adjacent if a == b]
        check(
            not duplicates,
            f"no redundant adjacent modal G0/G1 words (found {len(duplicates)})",
        )

        check(estimate_runtime(plan, WEGSTR_LIGHT, config) != "", "runtime estimate produced")
        check(program.count("G0 Z2") > 0, "rapid retracts to Z=2.0 mm")

        match = re.search(r"Total cutting distance: ([0-9.]+) mm", program)
        check(match is not None, "program reports a total cutting distance")
        if match:
            emitted = float(match.group(1))
            check(
                abs(emitted - plan.total_cut_length) < 1.0,
                f"emitted total ({emitted:.1f} mm) matches plan "
                f"({plan.total_cut_length:.1f} mm)",
            )
    finally:
        project.cleanup()


def test_verification(archive: Path) -> None:
    print("\n== G-code verification ==")
    project = load_project(archive)
    try:
        config = SlicerConfig()
        plan = plan_toolpaths(project, config)
        report = verify_plan(plan, config, WEGSTR_LIGHT)
        check(report.ok, f"generated package verifies clean ({report.headline()})")

        stats = report.stats
        check(stats["cut_distance"] > 0, "verifier measured cutting distance")
        check(stats["lines"] > 0, "verifier counted lines")
        bounds = stats["xy_bounds"]
        check(bounds is not None, "verifier reported a toolpath envelope")
        if bounds:
            check(bounds[0] >= -0.001, f"toolpath X min {bounds[0]:.3f} is not negative")
            check(bounds[1] >= -0.001, f"toolpath Y min {bounds[1]:.3f} is not negative")
            check(
                bounds[2] <= WEGSTR_LIGHT.work_x + 0.001,
                f"toolpath X max {bounds[2]:.3f} inside the envelope",
            )
            check(
                bounds[3] <= WEGSTR_LIGHT.work_y + 0.001,
                f"toolpath Y max {bounds[3]:.3f} inside the envelope",
            )

        good = emit_plan(plan, config, WEGSTR_LIGHT, "faults")
        faults = {
            "rapid move with the tool down": good.replace("G0 Z2\n", "G0 Z-1\n", 1),
            "coordinate outside the work area": good.replace(
                "G90", "G90\nG0 X999 Y0", 1
            ),
            "missing program end": good.replace("M30", "", 1),
            "cutting with the spindle off": good.replace("M3 S12000", "", 1),
        }
        for label, broken in faults.items():
            check(broken != good, f"fault injection applied: {label}")
            result = verify_program(broken, WEGSTR_LIGHT, config.cutout_depth)
            check(not result.ok, f"verifier rejects: {label}")
    finally:
        project.cleanup()


def test_export_package(archive: Path) -> None:
    print("\n== Export package ==")
    import tempfile

    project = load_project(archive)
    try:
        config = SlicerConfig()
        config.mill_bottom = True
        plan = plan_toolpaths(project, config)

        pins = select_registration_holes(plan.board, 2)
        check(len(pins) == 2, f"found {len(pins)} registration pin candidates")
        check(
            all(p.diameter >= 0.8 for p in pins),
            "registration pins are large enough to take a drill shank",
        )

        with tempfile.TemporaryDirectory() as temp:
            package = write_package(
                plan, config, WEGSTR_LIGHT, Path(temp), "testboard"
            )
            names = [f.filename for f in package.files]
            check(len(names) >= 3, f"package contains {len(names)} files")
            check(
                names == sorted(names),
                "files are numbered in execution order",
            )
            check(any("drill" in n for n in names), "a dedicated drill file is written")
            check(any("isolation" in n for n in names), "a dedicated isolation file is written")
            check(
                any("board_cutout" in n for n in names),
                "a dedicated board_cutout file is written",
            )
            check(
                sum(1 for f in package.files if f.requires_flip_before) == 1,
                "exactly one file is marked as needing a board flip",
            )
            check(
                all(f.path is not None and f.path.exists() for f in package.files),
                "every file exists on disk",
            )
            check(
                package.readme is not None and package.readme.exists(),
                "README.md is written",
            )
            check(
                package.verification is not None and package.verification.ok,
                "exported package verifies clean",
            )

            readme = package.readme.read_text(encoding="utf-8")
            for expected in (
                "Machining guide",
                "Setup checklist",
                "FLIP THE BOARD",
                "Verification report",
                "flip axis",
            ):
                check(expected in readme, f"guide mentions {expected!r}")

        first = package.files[0]
        check(
            first.kind == "drill",
            f"drilling runs first (got {first.kind!r}) so holes can be used as pins",
        )
        check(
            package.files[-1].kind == "cutout",
            "outline is cut last so the part stays attached",
        )
    finally:
        project.cleanup()


def test_silkscreen_and_alignment(archive: Path) -> None:
    print("\n== Silkscreen and alignment pins ==")
    import tempfile

    import shapely
    from shapely import affinity
    from shapely.geometry import Point

    project = load_project(archive)
    try:
        config = SlicerConfig()
        config.mill_bottom = True
        config.silkscreen_enabled = True
        config.alignment_holes = True
        plan = plan_toolpaths(project, config)

        kinds = [g.kind for g in plan.groups]
        check("silkscreen" in kinds, "silkscreen operation generated")
        check(
            any(h.registration for g in plan.groups for h in g.holes),
            "alignment pins present in the plan",
        )

        silk = next(g for g in plan.groups if g.kind == "silkscreen")
        check(len(silk.polylines) > 0, f"silkscreen produced {len(silk.polylines)} paths")
        check(
            abs(silk.depth.total_depth - config.silkscreen_depth) < 1e-9,
            "silkscreen uses the configured depth",
        )

        pin_groups = [g for g in plan.groups if g.kind == "alignment"]
        check(len(pin_groups) == 1, f"one alignment group ({len(pin_groups)})")
        align = pin_groups[0]
        pin_holes = align.holes
        check(plan.groups[0] is align, "alignment pins are the very first operation")
        check(align.skip_tool_change, "alignment pins ask for no tool change")
        check(len(pin_holes) == 2, f"two alignment pins ({len(pin_holes)})")
        check(
            all(abs(h.depth - config.alignment_hole_depth) < 1e-9 for h in pin_holes),
            f"pins carry their own {config.alignment_hole_depth:.0f} mm depth",
        )

        drilled = sorted((round(h.x, 3), round(h.y, 3)) for h in pin_holes)
        wanted = sorted((round(x, 3), round(y, 3)) for x, y in config.pins())
        check(drilled == wanted, f"pins drilled at the configured positions {drilled}")

        axis_kind, axis_coordinate = pin_axis(*config.pins())
        board_centre = plan.board.outline.bounds[0] + plan.board.width / 2.0
        check(
            axis_kind == "vertical",
            f"the two pins imply a vertical flip axis (got {axis_kind})",
        )
        check(
            abs(axis_coordinate - board_centre) < 0.5,
            f"flip axis X={axis_coordinate:.2f} passes through the board centre "
            f"X={board_centre:.2f}",
        )
        span = abs(config.alignment_pin2_x - config.alignment_pin1_x)
        check(
            abs(span - config.alignment_pin_span) < 1e-6,
            f"pins are {span:.1f} mm apart",
        )
        top = plan.board.outline.bounds[3]
        pin_y = min(config.alignment_pin1_y, config.alignment_pin2_y)
        check(
            pin_y - top >= config.alignment_pin_clearance - 1e-6,
            f"pins sit {pin_y - top:.1f} mm above the board top",
        )

        pin1, pin2 = config.pins()
        moved = reflect_about_pin_axis(Point(*pin1), pin1, pin2)
        check(
            abs(moved.x - pin2[0]) < 1e-6 and abs(moved.y - pin2[1]) < 1e-6,
            "flipping maps pin 1 exactly onto pin 2",
        )

        minx, miny, maxx, maxy = plan.board.outline.bounds
        centre = (
            (minx + maxx) / 2.0 if axis_kind == "vertical" else (miny + maxy) / 2.0
        )
        check(
            abs(centre - axis_coordinate) < 0.5,
            f"board is centred on the flip axis ({centre:.2f} vs "
            f"{axis_coordinate:.2f})",
        )
        check(
            all(
                0 <= h.x <= WEGSTR_LIGHT.work_x and 0 <= h.y <= WEGSTR_LIGHT.work_y
                for h in pin_holes
            ),
            "alignment pins are inside the work area",
        )

        unmirrored_config = SlicerConfig()
        unmirrored_config.mill_bottom = True
        unmirrored_config.mirror_bottom = False
        unmirrored = plan_toolpaths(project, unmirrored_config)

        mirrored_config = SlicerConfig()
        mirrored_config.mill_bottom = True
        mirrored_config.mirror_bottom = True
        mirrored = plan_toolpaths(project, mirrored_config)

        omx, omy, oxx, oxy = mirrored.board.outline.bounds
        flip_axis = (
            (omy + oxy) / 2.0
            if config.flip_axis == "horizontal"
            else (omx + oxx) / 2.0
        )
        bottom_mirrored = mirrored.board.copper["bottom"]
        bottom_plain = unmirrored.board.copper["bottom"]
        if config.flip_axis == "horizontal":
            restored = affinity.scale(
                bottom_mirrored, xfact=1.0, yfact=-1.0, origin=(0.0, flip_axis)
            )
        else:
            restored = affinity.scale(
                bottom_mirrored, xfact=-1.0, yfact=1.0, origin=(flip_axis, 0.0)
            )
        union = shapely.union_all([restored, bottom_plain]).area
        overlap = shapely.intersection(restored, bottom_plain).area
        iou = overlap / union if union > 0 else 0.0
        check(iou > 0.99, f"back copper is an exact mirror of the front (IoU {iou:.3f})")

        with tempfile.TemporaryDirectory() as temp:
            package = write_package(plan, config, WEGSTR_LIGHT, Path(temp), "b")
            names = [f.filename for f in package.files]
            for expected in ("drill", "isolation", "silkscreen", "board_cutout"):
                check(
                    any(expected in n for n in names),
                    f"a {expected} file is written",
                )
            check(
                package.verification is not None and package.verification.ok,
                "silkscreen + alignment package verifies clean",
            )
            readme = package.readme.read_text(encoding="utf-8")
            check(
                "alignment pins" in readme.lower(),
                "guide explains the alignment pins",
            )

            names = [f.filename for f in package.files]
            cutouts = [n for n in names if "silkscreen_cutout" in n]
            check(
                len(cutouts) == 2,
                f"a silkscreen cut-out file per side ({cutouts})",
            )
            check(
                names[0].endswith("alignment.gcode"),
                f"alignment is its own file and runs first ({names[0]})",
            )
            alignment_file = package.files[0]
            check(
                "M0" not in alignment_file.program,
                "alignment file contains no tool-change pause",
            )
            check(
                alignment_file.program.count("Z-10") >= 2,
                "both pins are drilled to the full 10 mm",
            )
            check(
                names[1].endswith("drill.gcode"),
                f"the main drill file follows ({names[1]})",
            )
            check(
                names[-1].endswith("board_cutout.gcode"),
                f"board cut-out runs last ({names[-1]})",
            )

            top_index = next(
                i
                for i, f in enumerate(package.files)
                if f.side == "top" and f.kind == "isolation"
            )
            bottom_index = next(
                i
                for i, f in enumerate(package.files)
                if f.side == "bottom" and f.kind == "isolation"
            )
            check(top_index < bottom_index, "top side is milled before the bottom")
            check(
                package.files[bottom_index].requires_flip_before,
                "the first bottom-side file carries the flip",
            )

            for side in ("top", "bottom"):
                iso = next(
                    i
                    for i, f in enumerate(package.files)
                    if f.side == side and f.kind == "isolation"
                )
                silk = next(
                    i
                    for i, f in enumerate(package.files)
                    if f.side == side and f.kind == "silkscreen"
                )
                check(
                    iso < silk,
                    f"{side}: silkscreen runs after the isolation",
                )

            check("Build sequence" in readme, "guide has a build sequence")
            check(
                "manual" in readme.lower(),
                "guide lists the manual silkscreen step",
            )
    finally:
        project.cleanup()


def test_gcode_reader(archive: Path) -> None:
    print("\n== G-code reader (viewer) ==")
    import tempfile

    from gcode_reader import read_program

    project = load_project(archive)
    try:
        config = SlicerConfig()
        plan = plan_toolpaths(project, config)
        program_text = emit_plan(plan, config, WEGSTR_LIGHT, "reader")

        parsed = read_program(program_text, name="reader")
        check(len(parsed.segments) > 0, f"parsed {len(parsed.segments)} move segments")
        check(parsed.stats["lines"] > 0, "counted lines")
        import numpy as np

        expected = 0.0
        for group in plan.groups:
            passes = max(1, len(group.depth.passes()))
            for polyline in group.polylines:
                length = polyline.length
                if polyline.closed and len(polyline.points) > 1:
                    length += float(
                        np.hypot(*(polyline.points[-1] - polyline.points[0]))
                    )
                expected += length * passes
        check(
            abs(parsed.stats["cut_distance"] - expected) < 5.0,
            f"reader's cutting distance ({parsed.stats['cut_distance']:.0f} mm) "
            f"matches the emitted toolpaths ({expected:.0f} mm)",
        )
        check(
            parsed.stats["plunge_distance"] > 0,
            f"separately measured {parsed.stats['plunge_distance']:.0f} mm of plunging",
        )
        check(parsed.stats["rapid_distance"] > 0, "measured rapid travel")
        check(parsed.stats["z_min"] < 0, "saw cutting depth")
        check(len(parsed.pauses) > 0, f"found {len(parsed.pauses)} tool-change pauses")

        bounds = parsed.stats["bounds"]
        check(bounds is not None, "reported bounds")
        if bounds:
            check(bounds[0] >= -0.001, "reader bounds are not negative")
            check(
                bounds[2] <= WEGSTR_LIGHT.work_x + 0.001,
                "reader bounds inside the envelope",
            )

        from exporter import build_programs

        drill_plan = plan_toolpaths(project, SlicerConfig())
        drill_group = next(g for g in drill_plan.groups if g.kind == "drill")
        files, _ = build_programs(
            drill_plan, SlicerConfig(), WEGSTR_LIGHT, "drill"
        )
        drill_file = next(f for f in files if f.kind == "drill")
        parsed_drill = read_program(drill_file.program, name="drill")
        plunges = [
            s
            for s in parsed_drill.segments
            if abs(s.x1 - s.x0) < 1e-9
            and abs(s.y1 - s.y0) < 1e-9
            and s.cutting
        ]
        check(
            len(plunges) >= len(drill_group.holes),
            f"found {len(plunges)} plunge moves for {len(drill_group.holes)} holes",
        )
        check(
            len(parsed_drill.stats["depths"]) > 1,
            f"drill file has {len(parsed_drill.stats['depths'])} distinct peck depths",
        )
    finally:
        project.cleanup()


def test_ipc(archive: Path) -> None:
    print("\n== JSON IPC bridge ==")
    requests = [
        {"id": 1, "cmd": "ping"},
        {"id": 2, "cmd": "load", "params": {"path": str(archive)}},
        {"id": 3, "cmd": "nonsense"},
        {"id": 4, "cmd": "validate", "params": {"config": {"isolation_passes": 0}}},
        {
            "id": 5,
            "cmd": "plan",
            "params": {"config": {"isolation_passes": 1, "rubout_enabled": False}},
        },
        {"id": 6, "cmd": "defaults"},
        {"id": 7, "cmd": "tools"},
        {"id": 8, "cmd": "select_tool", "params": {"role": "cutout", "key": "nope"}},
    ]
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"

    proc = subprocess.run(
        [sys.executable, str(HERE / "main.py"), "--ipc"],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    check(proc.returncode == 0, "IPC process exited cleanly")

    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    check(len(replies) == len(requests), f"one reply per request ({len(replies)})")

    by_id = {r["id"]: r for r in replies}
    check(by_id[1]["ok"] and by_id[1]["result"]["pong"], "ping responds")
    check(by_id[2]["ok"] and by_id[2]["result"]["layers"], "load returns layers")
    check(not by_id[3]["ok"], "unknown command returns an error, not a crash")
    check(
        not by_id[4]["result"]["ok"] and by_id[4]["result"]["problems"],
        "validate reports config problems",
    )
    result = by_id[5]["result"]
    check(result["board"]["width"] > 0, "plan returns board dimensions")
    check(len(result["groups"]) > 0, "plan returns toolpath groups")
    first = result["groups"][0]
    check(isinstance(first["polylines"][0]["points"][0], list), "polyline points are [x, y] pairs")
    check(result["summary"]["estimated_runtime"] != "", "plan includes a runtime estimate")

    defaults = by_id[6]["result"]
    check(defaults["version"].startswith("1."), f"defaults reports version {defaults['version']}")
    check("tool_db" in defaults, "defaults includes the tool database section")
    check(
        set(defaults["tools"]) >= {"isolation", "rubout", "cutout", "silkscreen"},
        "defaults lists every tool role",
    )

    tools = by_id[7]["result"]
    if tools.get("available"):
        check(tools["count"] > 0, f"tools command lists {tools['count']} tools")
        check(bool(tools["recommended"]["isolation"]), "tools command recommends an isolation tool")
    else:
        print("  [skip] no tool database on this machine")

    check(not by_id[8]["ok"], "selecting an unknown tool reports an error")


def test_tool_db() -> None:
    print("\n== Vectric tool database ==")
    found = find_tool_db()
    if found is None:
        print("  [skip] no .vtdb tool database on this machine")
        return

    database = load_tool_db()
    check(len(database) >= 5, f"loaded {len(database)} tools from {found.name}")
    check(bool(database.machine), f"machine name read: {database.machine!r}")
    check(
        all(tool.kind in ("flat", "vbit") for tool in database),
        "every tool maps to a flat or V-bit cutter",
    )
    check(
        all(tool.name for tool in database),
        "every tool has a display name",
    )
    vbits = database.vbits()
    check(bool(vbits) and all(t.angle > 0 for t in vbits), "V-bits carry an included angle")
    with_data = [t for t in database if t.feed_rate > 0]
    check(bool(with_data), f"{len(with_data)} tools carry Wegstr cutting data")
    check(
        all(t.spindle_speed > 0 for t in with_data),
        "cutting data includes spindle speeds",
    )

    fallback = recommend_tools(database)
    check(
        fallback["roles"]["isolation"] is not None,
        "recommends an isolation tool without board data",
    )

    tight = recommend_tools(database, isolation_depth=0.05, min_gap=0.15)
    iso = tight["roles"]["isolation"]
    check(iso is not None, "recommends an isolation tool for a 0.15 mm clearance")
    if iso is not None:
        tool = database.by_key(tight["keys"]["isolation"])
        check(
            tool is not None and tool.cut_width(0.05) <= 0.15 + 1e-9,
            f"recommended tool cuts {tool.cut_width(0.05):.3f} mm, inside the clearance",
        )

    config = SlicerConfig()
    chosen = database.by_key(fallback["keys"]["isolation"])
    apply_role(config, "isolation", chosen)
    check(
        config.isolation_tool.source == chosen.key
        and config.isolation_tool.feed_rate == chosen.feed_rate,
        "applying a database tool copies its geometry and feeds",
    )
    check(
        config.isolation_tool.resolved_feeds(WEGSTR_LIGHT)[0] > 0,
        "resolved feed rate is usable",
    )


def test_clearance_and_feeds(archive: Path) -> None:
    print("\n== Copper clearance and per-tool feeds ==")
    project = load_project(archive)
    try:
        copper = project.layer(LayerType.TOP_COPPER)
        gap = estimate_min_clearance(copper.geometry)
        check(gap is not None and 0.05 < gap < 2.0, f"measured copper clearance {gap:.3f} mm")

        config = SlicerConfig()
        config.mill_bottom = False
        config.isolation_tool = ToolSpec(
            "test", "flat", 0.3, feed_rate=123.0, plunge_rate=45.0, spindle_rpm=13000
        )
        plan = plan_toolpaths(project, config)
        program = emit_plan(plan, config, program_name="feeds")
        check("F123" in program, "tool feed rate appears in the G-code")
        check("S13000" in program, "tool spindle speed appears in the G-code")
        check("F45" in program, "tool plunge rate appears in the G-code")
        check(
            config.isolation_tool.resolved_feeds(WEGSTR_LIGHT)[2] == 13000,
            "per-tool spindle speed overrides the profile default",
        )
    finally:
        project.cleanup()


def test_updater_versions() -> None:
    print("\n== Update checker ==")
    check(parse_version("v1.2.3") == (1, 2, 3), "parses a v-prefixed version")
    check(parse_version("1.10.0") > parse_version("1.9.9"), "compares numerically, not lexically")
    check(parse_version("2.0.0-beta1") == (2, 0, 0, 1), "parses a prerelease tag")


def test_sqlite_reader() -> None:
    print("\n== Pure-Python SQLite reader ==")
    from sqlite_read import SqliteError, SqliteReader

    found = find_tool_db()
    if found is None:
        print("  [skip] no .vtdb tool database on this machine")
        return

    reader = SqliteReader(found)
    try:
        tables = reader.tables()
        check("tool_geometry" in tables, f"schema lists {len(tables)} tables")
        check(reader.has_table("tool_cutting_data"), "finds the cutting-data table")
        check(not reader.has_table("no_such_table"), "reports missing tables correctly")

        columns = reader.columns("tool_geometry")
        for expected in ("id", "name_format", "tool_type", "diameter", "included_angle"):
            check(expected in columns, f"column {expected!r} parsed from the CREATE statement")

        geometry = reader.rows("tool_geometry")
        check(len(geometry) == 21, f"read {len(geometry)} tool_geometry rows")
        check(
            all("id" in row and "diameter" in row for row in geometry),
            "rows are keyed by column name",
        )
        types = {row.get("tool_type") for row in geometry}
        check(types <= {1, 2, 3, 4}, f"tool_type values decoded as integers: {sorted(types)}")
        names = [row.get("name_format") for row in geometry]
        check(
            any(isinstance(n, str) and n for n in names),
            "text values decoded as strings",
        )

        cutting = reader.rows("tool_cutting_data")
        check(len(cutting) == 42, f"read {len(cutting)} tool_cutting_data rows")
        speeds = [
            row.get("spindle_speed")
            for row in cutting
            if row.get("spindle_speed") is not None
        ]
        check(
            all(isinstance(s, int) for s in speeds),
            "integer columns stay integers (no float coercion)",
        )
    finally:
        reader.close()

    try:
        SqliteReader(HERE / "test_pipeline.py")
        check(False, "non-SQLite input raises SqliteError")
    except SqliteError:
        check(True, "non-SQLite input raises SqliteError")


def test_start_menu_shortcut() -> None:
    print("\n== Start-menu registration ==")
    import sys
    import tempfile

    root = HERE.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        from app.shortcuts import (
            SHORTCUT_NAME,
            current_target,
            install_shortcut,
            is_current,
            remove_shortcut,
            shortcut_path,
            start_menu_dir,
            target_key,
        )
    except Exception as exc:
        check(False, f"shortcut module imports ({exc})")
        return

    if sys.platform != "win32":
        print("  [skip] Start-menu shortcuts are Windows-only")
        return

    menu = start_menu_dir()
    check(menu is not None and menu.is_dir(), f"found the Start-menu folder: {menu}")
    check(
        shortcut_path() is not None and shortcut_path().name == SHORTCUT_NAME,
        "shortcut path resolves to a .lnk in the Start menu",
    )

    target = current_target()
    check(target is not None, "resolved something to point the shortcut at")
    if target is not None:
        executable, arguments, working_dir, _icon = target
        check(executable.exists(), f"shortcut target exists: {executable.name}")
        check(working_dir.is_dir(), "shortcut working directory exists")
        check(
            isinstance(arguments, str),
            f"shortcut arguments resolved ({arguments or 'none'})",
        )

    with tempfile.TemporaryDirectory(prefix="wegstr_lnk_") as tmp:
        directory = Path(tmp)
        created = install_shortcut(directory=directory, target=target)
        check(created is not None and created.exists(), "created a shortcut in a temp folder")
        check(
            created is not None and created.suffix.lower() == ".lnk",
            "the shortcut has a .lnk extension",
        )

        key = target_key(target)
        check(bool(key) and "|" in key, f"target key records the exe and arguments: {key}")
        check(
            is_current(directory=directory, target=target, recorded=key),
            "a freshly created shortcut is reported as current",
        )
        check(
            not is_current(directory=directory, target=target, recorded="C:/gone|"),
            "a shortcut pointing at an old location is reported as stale",
        )

        check(remove_shortcut(directory), "removed the temp shortcut")
        check(
            created is not None and not created.exists(),
            "the shortcut is gone after removal",
        )
        check(
            not is_current(directory=directory, target=target, recorded=key),
            "a missing shortcut is reported as stale, so it gets rebuilt",
        )


def test_file_by_file() -> None:
    print("\n== Loading individual Gerber / drill files ==")
    import tempfile

    from gerber_io import load_files
    from sample_gerber import write_sample_gerber

    with tempfile.TemporaryDirectory(prefix="wegstr_files_") as tmp:
        directory = Path(tmp) / "gerber"
        write_sample_gerber(directory)
        names = sorted(p.name for p in directory.iterdir() if p.suffix.lower() != ".zip")
        check(len(names) >= 6, f"sample board has {len(names)} separate files")

        paths = [directory / name for name in names]
        project = load_files(paths)
        try:
            roles = {layer.layer_type for layer in project.layers}
            check(LayerType.EDGE_CUTS in roles, "outline recognised from a loose file")
            check(LayerType.TOP_COPPER in roles, "front copper recognised from a loose file")
            check(LayerType.BOTTOM_COPPER in roles, "back copper recognised from a loose file")
            check(LayerType.TOP_MASK in roles, "solder mask recognised from a loose file")
            check(len(project.drills) == 2, f"two drill programs from loose files ({len(project.drills)})")
            check(
                sum(len(d.holes) for d in project.drills) > 0,
                "loose drill files yielded holes",
            )
        finally:
            project.cleanup()

        partial = load_files(paths[:2])
        try:
            check(
                len(partial.layers) + len(partial.drills) >= 1,
                "a partial set still loads something",
            )
            check(
                any("No board outline" in w or "No front copper" in w for w in partial.warnings),
                "a partial set warns about what is missing",
            )
        finally:
            partial.cleanup()

        duplicate = load_files(paths + paths)
        try:
            names_loaded = [layer.name for layer in duplicate.layers]
            check(
                len(names_loaded) == len(set(names_loaded)),
                f"duplicate paths are de-duplicated ({len(names_loaded)} files)",
            )
        finally:
            duplicate.cleanup()

        try:
            load_files([directory / "nope.GTL"])
            check(False, "a missing file raises FileNotFoundError")
        except FileNotFoundError:
            check(True, "a missing file raises FileNotFoundError")

        override = load_files(
            [directory / "Sample_TopLayer.GTL"], roles={"Sample_TopLayer.GTL": "edge_cuts"}
        )
        try:
            check(
                override.layer(LayerType.EDGE_CUTS) is not None,
                "an explicit role override is honoured",
            )
        finally:
            override.cleanup()


def test_mcp_bridge() -> None:
    print("\n== MCP server ==")
    import json
    import tempfile

    from sample_gerber import write_sample_gerber

    with tempfile.TemporaryDirectory(prefix="wegstr_mcp_") as tmp:
        root = Path(tmp)
        archive = write_sample_gerber(root / "gerber")
        out_dir = root / "out"

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "open_archive", "arguments": {"path": str(archive)}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "plan", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "export", "arguments": {"out_dir": str(out_dir)}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "python", "arguments": {"code": "print('ok'); state.describe()"}}},
            {"jsonrpc": "2.0", "id": 7, "method": "resources/read", "params": {"uri": "wegstr://skill"}},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "add_files", "arguments": {"paths": [str(root / "gerber" / "Sample_TopLayer.GTL"), str(root / "gerber" / "Sample_BoardOutlineLayer.GKO")], "roles": {}}}},
        ]
        payload = "\n".join(json.dumps(r) for r in requests) + "\n"
        proc = subprocess.run(
            [sys.executable, str(HERE / "mcp_server.py")],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )
        check(proc.returncode == 0, "MCP server exited cleanly")
        replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        check(len(replies) == len(requests) - 1, f"one reply per request ({len(replies)})")
        by_id = {r["id"]: r for r in replies if "id" in r}

        init = by_id[1]["result"]
        check(init["serverInfo"]["name"] == "wegstr-pcb-slicer", "server identifies itself")
        check(init["protocolVersion"] == "2025-06-18", "protocol version echoed")

        tools = {t["name"] for t in by_id[2]["result"]["tools"]}
        for expected in ("open_archive", "add_files", "plan", "export", "python", "list_tools"):
            check(expected in tools, f"tool {expected!r} exposed")
        check(len(tools) >= 15, f"{len(tools)} tools exposed")

        def payload_of(request_id):
            return json.loads(by_id[request_id]["result"]["content"][0]["text"])

        project = payload_of(3)
        check(len(project["layers"]) >= 4, "open_archive returns layers over MCP")
        plan = payload_of(4)
        check(len(plan["groups"]) >= 2, "plan returns operations over MCP")
        export = payload_of(5)
        check(bool(export["files"]), f"export wrote {len(export['files'])} file(s)")
        check(out_dir.exists(), "export directory exists")

        python = payload_of(6)
        check(python["stdout"].strip() == "ok", "python tool runs code")
        check(isinstance(python.get("result"), str) and "layers" in python["result"], "python tool returns the live state")

        skill = by_id[7]["result"]["contents"][0]["text"]
        check("Wegstr" in skill and len(skill) > 300, "skill resource is served")
        check(payload_of(8)["layers"], "add_files accepts a role map")


def test_machine_envelope() -> None:
    print("\n== Machine work area ==")
    import tempfile

    from gerber_io import load_files
    from wegstr_gcode import WEGSTR_LIGHT, MachineProfile

    profile = MachineProfile()
    check(
        abs(profile.work_x - 140.0) < 1e-6 and abs(profile.work_y - 200.0) < 1e-6,
        f"Wegstr Light work area is {profile.work_x:.0f} x {profile.work_y:.0f} mm",
    )
    check(
        abs(WEGSTR_LIGHT.work_y - 200.0) < 1e-6,
        "the shipped WEGSTR_LIGHT profile uses 200 mm",
    )
    config = SlicerConfig()
    check(
        abs(config.machine_x - 140.0) < 1e-6 and abs(config.machine_y - 200.0) < 1e-6,
        f"slicer defaults match ({config.machine_x:.0f} x {config.machine_y:.0f} mm)",
    )

    gerber = (
        "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\nG01*\nG90*\n%ADD10C,0.150000*%\n"
        "D10*\nX0Y0D02*\nX60000000Y0D01*\nX60000000Y150000000D01*\n"
        "X0Y150000000D01*\nX0Y0D01*\nM02*\n"
    )
    with tempfile.TemporaryDirectory(prefix="wegstr_tall_") as tmp:
        outline = Path(tmp) / "tall.GKO"
        outline.write_text(gerber, encoding="utf-8", newline="\n")
        project = load_files([outline])
        try:
            check(
                abs(project.outline.bounds[3] - project.outline.bounds[1] - 150.0) < 0.5,
                "test board is 150 mm tall",
            )
            plan = plan_toolpaths(project, SlicerConfig())
            check(
                plan.board.height > 140.0,
                f"a 150 mm tall board plans on the 200 mm bed ({plan.board.height:.1f} mm)",
            )
            try:
                plan_toolpaths(project, SlicerConfig(machine_y=90.0))
                check(False, "the old 90 mm limit should have rejected it")
            except SlicerError:
                check(True, "the old 90 mm limit rejects the same board")
        finally:
            project.cleanup()


def test_rotation_and_pins() -> None:
    print("\n== Auto-rotation and pin placement ==")
    import tempfile

    from gerber_io import load_files

    def rectangle(width: float, height: float) -> str:
        def c(v: float) -> int:
            return int(round(v * 1_000_000))

        return (
            "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\nG01*\nG90*\n%ADD10C,0.150000*%\n"
            f"D10*\nX0Y0D02*\nX{c(width)}Y0D01*\nX{c(width)}Y{c(height)}D01*\n"
            f"X0Y{c(height)}D01*\nX0Y0D01*\nM02*\n"
        )

    with tempfile.TemporaryDirectory(prefix="wegstr_rot_") as tmp:
        wide = Path(tmp) / "wide.GKO"
        wide.write_text(rectangle(150.0, 90.0), encoding="utf-8", newline="\n")
        project = load_files([wide])
        try:
            config = SlicerConfig()
            config.alignment_holes = False
            config.mill_bottom = False
            plan = plan_toolpaths(project, config)
            check(plan.board.rotation == 90, f"rotated {plan.board.rotation} degrees to fit")
            check(
                abs(plan.board.width - 90.0) < 1.0 and abs(plan.board.height - 150.0) < 1.0,
                f"board became {plan.board.width:.1f} x {plan.board.height:.1f} mm",
            )
            check(
                plan.board.width <= config.machine_x and plan.board.height <= config.machine_y,
                "rotated board fits the bed",
            )
            check(
                any("Rotated the board" in w for w in plan.warnings),
                "rotation is reported",
            )

            config = SlicerConfig()
            config.alignment_holes = True
            config.alignment_pins_auto = True
            config.alignment_hole_diameter = 2.0
            config.mill_bottom = False
            plan = plan_toolpaths(project, config)
            pins = plan.board.pins
            check(pins is not None and plan.board.pins_auto, "auto placement is reported")
            if pins is not None:
                check(
                    abs(abs(pins[1][0] - pins[0][0]) - 80.0) < 1e-6,
                    "auto pins are exactly 80 mm apart",
                )
                top = plan.board.outline.bounds[3]
                gap = min(y for _, y in pins) - top
                check(
                    gap >= 5.0 - 1e-6,
                    f"auto pins sit {gap:.1f} mm above the board top (>= 5 mm)",
                )
                centre = (pins[0][0] + pins[1][0]) / 2.0
                board_centre = plan.board.outline.bounds[0] + plan.board.width / 2.0
                check(
                    abs(centre - board_centre) < 0.5,
                    "auto placement centres the board on the flip axis",
                )
                check(
                    all(0.0 <= x <= config.machine_x and 0.0 <= y <= config.machine_y for x, y in pins),
                    "auto pins stay on the bed",
                )
            alignment = next(g for g in plan.groups if g.kind == "alignment")
            check(
                abs(alignment.tool.diameter - 2.0) < 1e-9,
                f"the alignment group drills {alignment.tool.diameter:.1f} mm holes",
            )
            check(
                any("Locating pins placed" in w for w in plan.warnings),
                "auto placement is reported in the warnings",
            )

            config = SlicerConfig()
            config.alignment_holes = True
            config.alignment_pins_auto = False
            config.mill_bottom = False
            config.alignment_pin1_x = 0.0
            config.alignment_pin2_x = 80.0
            config.alignment_pin1_y = 0.0
            config.alignment_pin2_y = 0.0
            plan = plan_toolpaths(project, config)
            pins = plan.board.pins
            check(pins is not None, "effective pins are recorded")
            if pins is not None:
                check(
                    all(0.0 <= x <= config.machine_x for x, _ in pins),
                    f"pins stay on the bed: {[(round(x, 1), round(y, 1)) for x, y in pins]}",
                )
                centre = (pins[0][0] + pins[1][0]) / 2.0
                check(
                    abs(centre - (plan.board.outline.bounds[0] + plan.board.width / 2.0)) < 0.5,
                    "board stays centred on the pin axis",
                )
            check(
                any("Moved the alignment pins" in w for w in plan.warnings),
                "manual pin adjustment is reported",
            )
            check(
                min(h.x for g in plan.groups for h in g.holes) >= 0.0
                if any(g.holes for g in plan.groups)
                else True,
                "no drilled hole lands at a negative coordinate",
            )

            config = SlicerConfig()
            config.alignment_holes = False
            config.mill_bottom = False
            config.auto_rotate = False
            try:
                plan_toolpaths(project, config)
                check(False, "auto_rotate=False should reject the 150 mm board")
            except SlicerError:
                check(
                    True,
                    "auto_rotate=False leaves it unrotated and rejects the 150 mm side",
                )

            config.rotation = 90
            plan = plan_toolpaths(project, config)
            check(
                plan.board.rotation == 90,
                "an explicit 90 degree rotation is honoured",
            )
            check(
                abs(plan.board.width - 90.0) < 1.0
                and abs(plan.board.height - 150.0) < 1.0,
                f"explicit 90 degrees gives {plan.board.width:.1f} x "
                f"{plan.board.height:.1f} mm",
            )

            config.rotation = 270
            plan = plan_toolpaths(project, config)
            check(
                plan.board.rotation == 270,
                "an explicit 270 degree rotation is honoured",
            )

            config.rotation = 180
            try:
                plan_toolpaths(project, config)
                check(False, "180 degrees should still be too wide")
            except SlicerError:
                check(True, "180 degrees leaves it 150 mm wide and is rejected")

            config.rotation = 270
            config.auto_rotate = True
            plan = plan_toolpaths(project, config)
            check(
                plan.board.rotation == 270,
                "an explicit rotation wins over auto-rotate",
            )
        finally:
            project.cleanup()


def test_copper_preview_shape(archive: Path) -> None:
    print("\n== Copper preview geometry ==")
    project = load_project(archive)
    try:
        config = SlicerConfig()
        config.mill_bottom = True
        plan = plan_toolpaths(project, config)
        groups = plan.board.copper_polygons()
        check(set(groups) <= {"top", "bottom"}, f"sides returned: {sorted(groups)}")
        for side, polygons in groups.items():
            check(isinstance(polygons, list), f"{side} returns a list of polygons")
            if not polygons:
                continue
            first = polygons[0]
            check(isinstance(first, list) and len(first) >= 1, f"{side} polygon has rings")
            check(
                all(len(ring) >= 4 and len(ring[0]) == 2 for ring in first),
                f"{side} rings are closed coordinate lists",
            )
            check(
                all(ring[0] == ring[-1] for ring in first),
                f"{side} rings are explicitly closed",
            )
        total = sum(len(p) for p in groups.values())
        check(total >= 1, f"{total} copper polygons available to the viewer")

        preview = plan.board.copper_preview()
        check(
            set(preview) == set(groups),
            "the flat ring preview and the polygon preview cover the same sides",
        )
    finally:
        project.cleanup()


def test_pin_origin() -> None:
    print("\n== Work origin at the top-left pin ==")
    import tempfile

    from gerber_io import load_files
    from wegstr_gcode import WEGSTR_LIGHT, emit_plan, work_envelope, work_origin

    def rectangle(width: float, height: float) -> str:
        def c(v: float) -> int:
            return int(round(v * 1_000_000))

        return (
            "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\nG01*\nG90*\n%ADD10C,0.150000*%\n"
            f"D10*\nX0Y0D02*\nX{c(width)}Y0D01*\nX{c(width)}Y{c(height)}D01*\n"
            f"X0Y{c(height)}D01*\nX0Y0D01*\nM02*\n"
        )

    with tempfile.TemporaryDirectory(prefix="wegstr_origin_") as tmp:
        outline = Path(tmp) / "board.GKO"
        outline.write_text(rectangle(150.0, 90.0), encoding="utf-8", newline="\n")
        project = load_files([outline])
        try:
            config = SlicerConfig()
            config.alignment_holes = True
            config.alignment_pins_auto = True
            config.mill_bottom = True
            config.origin_mode = "pin_top_left"
            plan = plan_toolpaths(project, config)

            pins = plan.board.pins
            check(pins is not None, "pins exist for the origin mode")
            origin_x, origin_y, y_down = work_origin(config, plan.board)
            check(y_down, "the pin origin flips Y so +Y runs down the machine")
            if pins is not None:
                check(
                    abs(origin_x - min(p[0] for p in pins)) < 1e-6,
                    f"origin X is the left pin ({origin_x:.2f})",
                )
                check(
                    abs(origin_y - pins[0][1]) < 0.5,
                    f"origin Y is the pin line ({origin_y:.2f})",
                )

            low_x, high_x, low_y, high_y = work_envelope(
                config, WEGSTR_LIGHT, plan.board
            )
            check(low_x < 0.0, f"the envelope allows the board's {low_x:.1f} mm overhang")
            check(high_x <= 140.0, f"the envelope still ends inside the bed ({high_x:.1f})")
            check(low_y <= 0.0 <= high_y, "Y zero sits inside the envelope")

            program = emit_plan(plan, config, program_name="origin-test")
            xs: list[float] = []
            ys: list[float] = []
            for line in program.splitlines():
                for token in line.split():
                    try:
                        if token.startswith("X"):
                            xs.append(float(token[1:]))
                        elif token.startswith("Y"):
                            ys.append(float(token[1:]))
                    except ValueError:
                        continue
            check(
                any("top-left alignment pin" in line for line in program.splitlines()),
                "the header documents the pin origin",
            )
            check(
                min(ys) >= -0.001,
                f"no Y move goes above the pin (min Y {min(ys):.2f})",
            )
            check(
                max(ys) <= 160.0,
                f"the board sits below the pin (max Y {max(ys):.2f})",
            )
            check(
                max(xs) - min(xs) > 80.0,
                f"X spans the board ({min(xs):.1f}..{max(xs):.1f})",
            )

            report = verify_plan(plan, config, WEGSTR_LIGHT)
            check(report.ok, "the pin-origin program verifies clean")
            bounds = report.stats.get("xy_bounds")
            check(bounds is not None, "verification reports bounds")
            if bounds:
                check(
                    bounds[0] < 0.0,
                    f"verification accepts the {bounds[0]:.1f} mm overhang",
                )
        finally:
            project.cleanup()


def test_custom_origin(archive: Path) -> None:
    print("\n== Picked work origin ==")
    from wegstr_gcode import WEGSTR_LIGHT, emit_plan, work_envelope, work_origin

    project = load_project(archive)
    try:
        config = SlicerConfig()
        config.mill_bottom = False
        config.alignment_holes = False
        plan = plan_toolpaths(project, config)
        minx, miny, maxx, maxy = plan.board.outline.bounds

        config.origin_mode = "custom"
        config.origin_custom_x = minx
        config.origin_custom_y = maxy
        config.rotation = 0
        plan = plan_toolpaths(project, config)

        origin_x, origin_y, y_down = work_origin(config, plan.board)
        check(
            abs(origin_x - minx) < 1e-6 and abs(origin_y - maxy) < 1e-6,
            f"origin sits on the picked point ({origin_x:.2f}, {origin_y:.2f})",
        )
        check(not y_down, "a picked origin keeps +Y up")

        low_x, high_x, low_y, high_y = work_envelope(config, WEGSTR_LIGHT, plan.board)
        check(
            abs((high_x - low_x) - WEGSTR_LIGHT.work_x) < 1e-6
            and abs((high_y - low_y) - WEGSTR_LIGHT.work_y) < 1e-6,
            "the envelope keeps the full bed size",
        )
        check(
            low_x <= 0.0 <= high_x and low_y <= 0.0 <= high_y,
            "zero stays inside the shifted envelope",
        )

        program = emit_plan(plan, config, program_name="custom-origin")
        xs: list[float] = []
        ys: list[float] = []
        for line in program.splitlines():
            for token in line.split():
                try:
                    if token.startswith("X"):
                        xs.append(float(token[1:]))
                    elif token.startswith("Y"):
                        ys.append(float(token[1:]))
                except ValueError:
                    continue
        check(min(xs) >= -0.001, f"X starts at zero (min {min(xs):.2f})")
        check(max(ys) <= 0.001, f"Y runs down from the origin (max {max(ys):.2f})")
        check(
            len(xs) > 0 and max(xs) - min(xs) > 1.0,
            f"the program spans {max(xs) - min(xs):.1f} mm in X",
        )

        report = verify_plan(plan, config, WEGSTR_LIGHT)
        check(report.ok, "a picked-corner origin verifies clean")
    finally:
        project.cleanup()


def test_double_sided_layout() -> None:
    print("\n== Double-sided files and mirroring ==")
    import tempfile

    from exporter import plan_export_files
    from pcb_engine import pin_axis
    from sample_gerber import write_sample_gerber

    with tempfile.TemporaryDirectory(prefix="wegstr_ds_") as tmp:
        archive = write_sample_gerber(Path(tmp) / "gerber")
        project = load_project(archive)
        try:
            def build(mirror: bool):
                config = SlicerConfig()
                config.mill_bottom = True
                config.alignment_holes = True
                config.mirror_bottom = mirror
                config.silkscreen_enabled = True
                config.silkscreen_bottom = True
                plan = plan_toolpaths(project, config)
                axis = pin_axis(*plan.board.pins)[1]
                return plan, axis, config

            plan, axis, config = build(True)
            files = plan_export_files(plan, config)
            names = [f.filename for f in files]
            check(
                any("isolation_top" in n for n in names)
                and any("isolation_bottom" in n for n in names),
                f"separate front and back isolation files exist",
            )
            check(
                sum(1 for n in names if "isolation" in n) == 2,
                f"exactly two isolation files ({sum(1 for n in names if 'isolation' in n)})",
            )
            check(
                any("silkscreen_cutout_top" in n for n in names)
                and any("silkscreen_cutout_bottom" in n for n in names),
                "separate front and back silkscreen files exist",
            )
            bottom = next(f for f in files if "isolation_bottom" in f.filename)
            check(bottom.requires_flip_before, "the back isolation file asks for the flip")
            check(
                bottom.side == "bottom",
                "the back isolation file is tagged bottom",
            )

            plain_plan, _, _ = build(False)
            plain = plain_plan.board
            mirrored = plan.board
            check(
                abs(axis - sum(p[0] for p in plan.board.pins) / 2.0) < 1e-6,
                f"the mirror line is the pin bisector at X={axis:.2f}",
            )
            check(
                plain.pins == mirrored.pins,
                "mirroring does not move the pins",
            )

            for store, label in (
                (mirrored.copper.get("bottom"), "copper"),
                (mirrored.silkscreen.get("bottom"), "silkscreen"),
            ):
                reference = plain.copper.get("bottom") if label == "copper" else plain.silkscreen.get("bottom")
                if store is None or reference is None:
                    check(False, f"{label} bottom geometry is available")
                    continue
                expected_low = 2 * axis - reference.bounds[2]
                expected_high = 2 * axis - reference.bounds[0]
                check(
                    abs(store.bounds[0] - expected_low) < 0.02
                    and abs(store.bounds[2] - expected_high) < 0.02,
                    f"{label} is mirrored about the pin line "
                    f"({store.bounds[0]:.2f}..{store.bounds[2]:.2f} vs "
                    f"{expected_low:.2f}..{expected_high:.2f})",
                )
        finally:
            project.cleanup()


def test_isolation_preset() -> None:
    print("\n== One-click isolation preset ==")
    from pcb_engine import apply_isolation_preset

    config = SlicerConfig()
    config.mill_bottom = True
    apply_isolation_preset(config, has_bottom=False)
    check(config.mill_top, "the preset always mills the front")
    check(
        not config.mill_bottom,
        "a board without back copper produces a single-sided preset",
    )
    check(config.isolation_passes == 1, "the preset uses one isolation pass")
    check(not config.rubout_enabled, "the preset leaves rub-out off")

    apply_isolation_preset(config, has_bottom=True)
    check(
        config.mill_bottom,
        "a board with back copper produces a two-sided preset",
    )


def main() -> int:
    import tempfile

    requested = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ARCHIVE
    temp_root: str | None = None
    if requested.exists():
        archive = requested
        print(f"Testing against {archive.name}")
    else:
        from sample_gerber import write_sample_gerber

        temp_root = tempfile.mkdtemp(prefix="wegstr_sample_")
        archive = write_sample_gerber(Path(temp_root) / "gerber")
        print(f"Testing against the generated sample board ({archive.name})")
    test_gerber_parsing(archive)
    test_engine(archive)
    test_gcode(archive)
    test_verification(archive)
    test_export_package(archive)
    test_silkscreen_and_alignment(archive)
    test_gcode_reader(archive)
    test_ipc(archive)
    test_sqlite_reader()
    test_tool_db()
    test_start_menu_shortcut()
    test_file_by_file()
    test_mcp_bridge()
    test_machine_envelope()
    test_rotation_and_pins()
    test_copper_preview_shape(archive)
    test_pin_origin()
    test_custom_origin(archive)
    test_double_sided_layout()
    test_isolation_preset()
    test_clearance_and_feeds(archive)
    test_updater_versions()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
