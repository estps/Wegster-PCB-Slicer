
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from gcode_verify import VerificationReport, verify_files
from pcb_engine import (
    SlicerConfig,
    ToolpathGroup,
    ToolpathPlan,
    pin_axis,
    select_registration_holes,
)
from wegstr_gcode import MachineProfile, emit_program

__all__ = [
    "ExportFile",
    "ExportPackage",
    "plan_export_files",
    "build_programs",
    "verify_plan",
    "write_package",
    "build_readme",
]


@dataclass
class ExportFile:

    index: int
    filename: str
    title: str
    side: str
    kind: str
    groups: list[ToolpathGroup] = field(default_factory=list)
    requires_flip_before: bool = False
    notes: list[str] = field(default_factory=list)
    program: str = ""
    path: Path | None = None
    estimated_minutes: float = 0.0

    @property
    def tools(self) -> list[str]:
        seen: list[str] = []
        for group in self.groups:
            if group.tool.name not in seen:
                seen.append(group.tool.name)
        return seen

    @property
    def cut_length(self) -> float:
        return sum(
            g.cut_length * max(1, len(g.depth.passes())) for g in self.groups
        )

    @property
    def hole_count(self) -> int:
        return sum(len(g.holes) for g in self.groups)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "filename": self.filename,
            "title": self.title,
            "side": self.side,
            "kind": self.kind,
            "tools": self.tools,
            "cut_length": round(self.cut_length, 2),
            "hole_count": self.hole_count,
            "requires_flip_before": self.requires_flip_before,
            "estimated_minutes": round(self.estimated_minutes, 1),
            "notes": self.notes,
        }


@dataclass
class ExportPackage:
    directory: Path
    stem: str
    files: list[ExportFile] = field(default_factory=list)
    readme: Path | None = None
    verification: VerificationReport | None = None
    double_sided: bool = False

    @property
    def total_minutes(self) -> float:
        return sum(f.estimated_minutes for f in self.files)

    def to_dict(self) -> dict:
        return {
            "directory": str(self.directory),
            "stem": self.stem,
            "double_sided": self.double_sided,
            "readme": str(self.readme) if self.readme else None,
            "files": [f.to_dict() for f in self.files],
            "total_minutes": round(self.total_minutes, 1),
            "verification": self.verification.to_dict() if self.verification else None,
        }


def _base_name(kind: str, side: str, double_sided: bool, config: SlicerConfig) -> str:
    suffix = f"_{side}" if double_sided else ""
    if kind == "isolation":
        return f"isolation{suffix}"
    if kind == "rubout":
        return f"rubout{suffix}"
    if kind == "silkscreen":
        if config.silkscreen_mode == "cutout":
            return f"silkscreen_cutout{suffix}"
        return f"silkscreen{suffix}"
    if kind == "alignment":
        return "alignment"
    if kind in ("drill", "registration"):
        return "drill"
    if kind == "cutout":
        return "board_cutout"
    return kind


def plan_export_files(plan: ToolpathPlan, config: SlicerConfig) -> list[ExportFile]:
    alignment_groups = [g for g in plan.groups if g.kind == "alignment"]
    drill_groups = [
        g for g in plan.groups if g.holes and g.kind != "alignment"
    ]
    trace_groups = [g for g in plan.groups if not g.holes]

    top = [g for g in trace_groups if g.side == "top"]
    bottom = [g for g in trace_groups if g.side == "bottom"]
    outline = [g for g in top if g.kind == "cutout"]
    top = [g for g in top if g.kind != "cutout"]

    sequence: list[tuple[str, str, list[ToolpathGroup], bool]] = []

    for group in alignment_groups:
        sequence.append(("alignment", group.name, [group], False))

    if drill_groups and config.drill_first:
        sequence.append(("drill", "Drill all holes", drill_groups, False))

    for group in top:
        sequence.append((group.kind, group.name, [group], False))

    flip_pending = bool(bottom) and bool(top or drill_groups)
    for group in bottom:
        sequence.append((group.kind, group.name, [group], flip_pending))
        flip_pending = False

    if drill_groups and not config.drill_first:
        sequence.append(("drill", "Drill all holes", drill_groups, False))

    for group in outline:
        sequence.append(("cutout", group.name, [group], False))

    double_sided = bool(config.mill_top and config.mill_bottom)
    files: list[ExportFile] = []
    for index, (kind, title, groups, flip) in enumerate(sequence, start=1):
        side = groups[0].side if groups else "top"
        base = _base_name(kind, side, double_sided, config)
        files.append(
            ExportFile(
                index=index,
                filename=f"{index:02d}_{base}.gcode",
                title=title,
                side=side,
                kind=kind,
                groups=groups,
                requires_flip_before=flip,
            )
        )
    return files


def _setup_note(export_file: ExportFile, double_sided: bool) -> str:
    if export_file.requires_flip_before:
        return (
            "FLIP THE BOARD OVER before running this file, then re-zero Z on the "
            "new top surface. See README.md."
        )
    if double_sided and export_file.side == "bottom":
        return "Back side - the artwork is mirrored so it lines up after flipping."
    return ""


def build_programs(
    plan: ToolpathPlan,
    config: SlicerConfig,
    profile: MachineProfile,
    stem: str,
) -> tuple[list[ExportFile], list[tuple[str, str]]]:
    double_sided = bool(config.mill_top and config.mill_bottom)

    if config.split_output:
        files = plan_export_files(plan, config)
    else:
        files = [
            ExportFile(
                index=1,
                filename=f"{stem}.gcode",
                title="Complete job",
                side="top",
                kind="combined",
                groups=list(plan.groups),
            )
        ]

    programs: list[tuple[str, str]] = []
    for export_file in files:
        program = emit_program(
            export_file.groups,
            config,
            profile,
            f"{stem} - {export_file.title}",
            board=plan.board,
            preamble=plan.warnings,
            setup_note=_setup_note(export_file, double_sided),
        )
        export_file.program = program
        programs.append((export_file.filename, program))

    max_depth = _max_cut_depth(plan, config)
    for export_file, (_, program) in zip(files, programs):
        report = verify_files([(export_file.filename, program)], profile, max_depth)
        export_file.estimated_minutes = report.stats.get("estimated_minutes", 0.0)

    return files, programs


def verify_plan(
    plan: ToolpathPlan,
    config: SlicerConfig,
    profile: MachineProfile,
    stem: str = "preview",
) -> VerificationReport:
    _, programs = build_programs(plan, config, profile, stem)
    return verify_files(programs, profile, _max_cut_depth(plan, config))


def write_package(
    plan: ToolpathPlan,
    config: SlicerConfig,
    profile: MachineProfile,
    out_dir: Path,
    stem: str,
) -> ExportPackage:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    package = ExportPackage(
        directory=out_dir,
        stem=stem,
        double_sided=bool(config.mill_top and config.mill_bottom),
    )
    package.files, programs = build_programs(plan, config, profile, stem)

    for export_file in package.files:
        export_file.path = out_dir / export_file.filename
        export_file.path.write_text(
            export_file.program, encoding="utf-8", newline="\n"
        )

    package.verification = verify_files(
        programs, profile, _max_cut_depth(plan, config)
    )

    readme = out_dir / "README.md"
    readme.write_text(
        build_readme(plan, config, profile, package, stem), encoding="utf-8", newline="\n"
    )
    package.readme = readme
    return package


def _max_cut_depth(plan: ToolpathPlan, config: SlicerConfig) -> float:
    depths = [abs(g.depth.total_depth) for g in plan.groups]
    if not depths:
        return max(config.isolation_depth, config.cutout_depth)
    return max(depths)


def _fmt_minutes(minutes: float) -> str:
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours} h {mins:02d} min"
    return f"{mins} min"


def build_readme(
    plan: ToolpathPlan,
    config: SlicerConfig,
    profile: MachineProfile,
    package: ExportPackage,
    stem: str,
) -> str:
    board = plan.board
    double = package.double_sided
    lines: list[str] = []
    add = lines.append

    add(f"# Machining guide — {stem}")
    add("")
    add(
        f"> Generated by **Wegstr PCB Slicer** on "
        f"{_dt.datetime.now().strftime('%Y-%m-%d at %H:%M')}  "
    )
    add(f"> Machine: **{profile.name}** ({profile.work_x:.0f} × {profile.work_y:.0f} × {profile.work_z:.0f} mm)")
    add("")
    add("This guide is generated from the actual toolpaths in this folder. "
        "Run the files **in the numbered order** — each one is a separate, "
        "independently runnable program.")
    add("")

    add("## 1. Job summary")
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| Board size | {board.width:.2f} × {board.height:.2f} mm |")
    add(f"| Board thickness | {config.board_thickness:.2f} mm |")
    add(f"| Sides | {'Front **and back** (double-sided)' if double else 'Front only'} |")
    offset_x, offset_y = board.offset
    margin_text = (
        f"{offset_x:.1f} mm"
        if abs(offset_x - offset_y) < 1e-6
        else f"{offset_x:.1f} mm (X) x {offset_y:.1f} mm (Y)"
    )
    add(f"| Origin | {config.origin_mode.replace('_', '-')} + {margin_text} margin |")
    add(f"| Files | {len(package.files)} |")
    add(f"| Total cutting distance | {plan.total_cut_length:.0f} mm |")
    add(f"| Total holes | {len(board.holes)} |")
    add(f"| Estimated machine time | **{_fmt_minutes(package.total_minutes)}** |")
    add("")

    add("## 2. Tools you need")
    add("")
    add("| Step | File | Tool | Work |")
    add("|---|---|---|---|")
    for export_file in package.files:
        tools = ", ".join(export_file.tools) or "—"
        work = []
        if export_file.hole_count:
            work.append(f"{export_file.hole_count} holes")
        if export_file.cut_length:
            work.append(f"{export_file.cut_length:.0f} mm of cutting")
        add(
            f"| {export_file.index} | `{export_file.filename}` | {tools} | "
            f"{', '.join(work) or '—'} |"
        )
    add("")

    add("## 3. Setup checklist")
    add("")
    add("- [ ] Blank is at least "
        f"**{board.width + 2 * offset_x:.0f} × {board.height + 2 * offset_y:.0f} mm** "
        "and flat.")
    add("- [ ] Blank is clamped/glued down flat, with a sacrificial spoilboard underneath.")
    add("- [ ] X and Y zeroed at the lower-left corner of the blank "
        f"(+{config.margin:.1f} mm margin).")
    add("- [ ] Z zeroed with the tool **just touching** the copper surface.")
    add("- [ ] Dust extraction on. FR4 dust is nasty — wear a mask.")
    add("- [ ] Spindle warmed up.")
    add("")
    add("> **Zeroing Z is the single most important step.** Too shallow and the "
        "copper is not cut through; too deep and you lose trace width. Re-check it "
        "before every file.")
    add("")

    add("## 4. Build sequence")
    add("")
    add("Run the files in this order. Steps marked **manual** are done by hand.")
    add("")
    step = 0
    for export_file in package.files:
        if export_file.requires_flip_before:
            step += 1
            add(f"{step}. **FLIP THE BOARD** — see section 6")
        step += 1
        add(f"{step}. `{export_file.filename}` — {export_file.title}")
        if export_file.kind == "isolation" and config.silkscreen_enabled:
            step += 1
            add(f"{step}. **manual** — apply the silkscreen to the "
                f"{export_file.side} side, then let it dry")
    add("")

    add("## 5. What each file does")
    add("")
    for export_file in package.files:
        add(f"### Step {export_file.index} — `{export_file.filename}`")
        add("")
        add(f"**{export_file.title}**")
        add("")
        if export_file.requires_flip_before:
            add("> ### ⚠️ FLIP THE BOARD BEFORE THIS FILE")
            add(">")
            add("> 1. Remove the blank from the machine.")
            add("> 2. Turn it over (left-to-right, i.e. about the vertical axis).")
            add("> 3. Re-clamp it in exactly the same place — or drop it back over "
                "the registration pins (see section 6).")
            add("> 4. **Re-zero Z** on the new top surface — it will not be the same height.")
            add("")
        add(f"- **Tool:** {', '.join(export_file.tools) or '—'}")
        if export_file.hole_count:
            add(f"- **Holes:** {export_file.hole_count}")
        if export_file.cut_length:
            add(f"- **Cutting distance:** {export_file.cut_length:.0f} mm")
        add(f"- **Expected time:** {_fmt_minutes(export_file.estimated_minutes)}")
        add("")
        add(_step_description(export_file, config))
        add("")
        for note in export_file.notes:
            add(f"- {note}")
        if export_file.notes:
            add("")

    if double:
        add("## 6. How the double-sided alignment works")
        add("")
        if config.alignment_holes:
            p1, p2 = config.pins()
            kind, coordinate = pin_axis(p1, p2)
            if kind == "vertical":
                axis_text = f"the vertical line **X = {coordinate:.1f} mm**"
            elif kind == "horizontal":
                axis_text = f"the horizontal line **Y = {coordinate:.1f} mm**"
            else:
                axis_text = "the line running halfway between the two pins"
            add(
                f"**The flip axis is {axis_text}** - the line halfway between the two "
                "alignment pins. Turning the board over swaps the pins over, so it drops "
                "back on in the same place and the back layer registers with the front."
            )
            add("")
            add(
                "The board is centred on that line automatically, so you never re-zero "
                "X or Y between the two sides - only Z."
            )
            add("")
        else:
            axis = (
                "left-to-right (about the vertical axis)"
                if config.flip_axis == "vertical"
                else "end-over-end (about the horizontal axis)"
            )
            add(
                f"**The board is turned over {axis}.** The back program is the front "
                "program mirrored about that same axis, so you never re-zero X or Y "
                "between the two sides - only Z."
            )
            add("")

        if config.alignment_holes:
            add("### Alignment pins (built into this job)")
            add("")
            pins = config.pins()
            add(
                f"`01_alignment.gcode` is its own file and runs first. It drills two "
                f"holes **straight down, {config.alignment_hole_depth:.0f} mm deep** at "
                f"**( {pins[0][0]:.1f}, {pins[0][1]:.1f} )** and "
                f"**( {pins[1][0]:.1f}, {pins[1][1]:.1f} )** - through the blank and "
                "into the spoilboard."
            )
            add("")
            add(
                "There is **no tool change** in that file. It uses whatever bit is "
                "already in the spindle, and that bit's size *is* your pin size. Drop "
                "shanks of that size into the two holes, then run the drill file with "
                "the real drill bits."
            )
            add("")
            add("1. Run `01_drill.gcode` and leave the blank clamped.")
            add("2. Push drill shanks (or dowel pins) through the two holes into the spoilboard.")
            add("3. Mill the front.")
            add("4. Lift the blank off the pins, turn it over, drop it back on. It can "
                "only go on one way.")
            add("5. **Re-zero Z only** - X and Y have not moved.")
            add("6. Mill the back. It lines up because it is a mirror of the front.")
            add("")
        else:
            add("### Method A - alignment pins (recommended)")
            add("")
            add(
                "Turn on **Alignment holes** in the app and re-export. The drill file "
                "will then start by cutting two deep pin holes on the flip axis, and "
                "registration becomes automatic."
            )
            add("")
            add("### Method B - existing through-holes")
            add("")
            add(
                "The same trick works with holes already on the board, as long as they "
                "are symmetrical about the flip axis. The two best candidates here are:"
            )
            add("")
            pins = select_registration_holes(board, config.registration_pins)
            if pins:
                add("| Pin | Diameter | Machine X | Machine Y |")
                add("|---|---|---|---|")
                for index, pin in enumerate(pins, start=1):
                    add(
                        f"| {index} | {pin.diameter:.3f} mm | {pin.x:.2f} | {pin.y:.2f} |"
                    )
                add("")
            else:
                add(
                    "_No through-hole at least 0.8 mm across was found, so pick two "
                    "convenient holes by eye._"
                )
                add("")
            add("1. Run `01_drill.gcode` first; it drills every hole before any "
                "copper is touched.")
            add("2. Push drill shanks through the two holes above and into the "
                "spoilboard underneath.")
            add("3. Mill the front, lift the board straight up off the pins, turn it "
                "over and drop it back on.")
            add("4. **Re-zero Z only**, then mill the back.")
            add("")
        add(f"### Method {'B' if config.alignment_holes else 'C'} - eyeball it")
        add("")
        add(
            "Without pins you are aligning by hand. Hold the board up to a light with "
            "the front artwork, line up two opposite through-holes, and accept roughly "
            "0.2 mm of error. Fine for a prototype, not for fine-pitch work."
        )
        add("")

    verification = package.verification
    if verification is not None:
        add(f"## {7 if double else 6}. Verification report")
        add("")
        stats = verification.stats
        add("Every file in this folder was re-read and checked against the machine "
            "limits after it was written:")
        add("")
        add("| Check | Result |")
        add("|---|---|")
        add(f"| Programs | {stats.get('files', len(package.files))} |")
        add(f"| Total lines | {stats.get('lines', 0)} |")
        add(f"| Cutting distance | {stats.get('cut_distance', 0):.0f} mm |")
        add(f"| Rapid distance | {stats.get('rapid_distance', 0):.0f} mm |")
        bounds = stats.get("xy_bounds")
        if bounds:
            add(
                f"| Toolpath envelope | X {bounds[0]:.2f} … {bounds[2]:.2f}, "
                f"Y {bounds[1]:.2f} … {bounds[3]:.2f} mm |"
            )
        add(f"| Deepest Z | {stats.get('z_min', 0):.3f} mm |")
        add(f"| Highest Z | {stats.get('z_max', 0):.3f} mm |")
        add(f"| Verdict | **{verification.headline()}** |")
        add("")
        if verification.issues:
            add("Issues found:")
            add("")
            for issue in verification.issues[:30]:
                add(f"- `{issue.severity.upper()}` (line {issue.line}) {issue.message}")
            add("")
        else:
            add("No problems found: every move is inside the work area, the spindle "
                "runs for every cut, nothing rapids with the tool down, and Z stays "
                "within the configured depth.")
            add("")

    number = (8 if double else 7)
    add(f"## {number}. If something goes wrong")
    add("")
    add("| Symptom | Likely cause | Fix |")
    add("|---|---|---|")
    add(
        "| Copper not cut through; traces short together | Z zeroed too high, or the "
        "blank is not flat | Re-zero Z on the actual surface; re-run the traces file "
        "with 0.02 mm more depth |"
    )
    add(
        "| Traces cut away / too thin | Z too deep, or the V-bit angle is set wrong | "
        "Reduce the isolation depth; check the V-bit included angle in Tooling |"
    )
    add(
        "| Tool snaps | Plunge feed too high, or a rapid move dragged the cutter | "
        "Lower the plunge feed; re-run the verification check |"
    )
    add(
        "| Drill wanders off-centre | Peck depth too deep for the bit, or the bit is "
        "dull | Reduce peck depth; use a fresh bit |"
    )
    add(
        "| Board shifts mid-cut | Not clamped firmly enough | Re-clamp; cut the "
        "outline file last so the part stays attached |"
    )
    add(
        "| Back side misaligned | Board not re-registered after flipping | Use the "
        "registration pin method in section 5 |"
    )
    add("")

    add("---")
    add("")
    add(
        f"*Generated by Wegstr PCB Slicer. Machine profile: {profile.name}, "
        f"cut feed {profile.cut_feed:.0f} mm/min, plunge feed "
        f"{profile.plunge_feed:.0f} mm/min, spindle {profile.spindle_rpm} RPM.*"
    )
    add("")

    return "\n".join(lines)


def _step_description(export_file: ExportFile, config: SlicerConfig) -> str:
    kind = export_file.kind
    has_alignment = any(g.kind == "alignment" for g in export_file.groups)

    if kind == "registration":
        return (
            "Drills the alignment pin holes through the blank and into the "
            "spoilboard. Do not move the blank until the job is finished."
        )
    if kind == "alignment":
        return (
            "Two holes, straight down, "
            f"{config.alignment_hole_depth:.0f} mm deep - through the blank and into "
            "the spoilboard. **No tool change**: it uses whatever bit is already in "
            "the spindle, and that bit's size becomes your pin size. Push shanks of "
            "that size into the holes before starting the next file."
        )
    if kind == "drill":
        text = (
            f"Peck-drills every hole to {config.board_thickness + config.drill_depth_extra:.2f} mm "
            f"in {config.peck_depth:.2f} mm pecks, retracting fully between pecks to clear chips. "
            "The machine will pause for each drill bit — swap bits when prompted."
        )
        if has_alignment:
            text = (
                f"**Starts by drilling the {config.alignment_hole_diameter:.1f} mm alignment "
                f"pins {config.alignment_hole_depth:.0f} mm deep** — through the blank and into "
                f"the spoilboard underneath. Drop drill shanks into those two holes and every "
                f"later operation lines up, on both sides. Then it {text[0].lower()}{text[1:]}"
            )
        return text
    if kind == "silkscreen":
        if config.silkscreen_mode == "cutout":
            return (
                f"Mills the silkscreen back off the pad openings, "
                f"{config.silkscreen_depth:.2f} mm deep, so the pads are exposed and "
                "will take solder. Small pads are cleared with a single plunge; "
                "larger openings are cleared with concentric passes."
            )
        text = (
            f"Engraves the silkscreen artwork {config.silkscreen_depth:.2f} mm deep, "
            "following the outline of the text and graphics."
        )
        if config.silkscreen_clear_pads:
            text += (
                f" A {config.silkscreen_pad_clearance:.2f} mm keep-out is left around "
                "every pad opening so solder still wets properly."
            )
        return text
    if kind == "isolation":
        depth = config.isolation_depth
        passes = config.isolation_passes
        side = "front" if export_file.side == "top" else "back"
        return (
            f"V-bit isolation of the {side} copper, {depth:.3f} mm deep in "
            f"{passes} pass{'es' if passes != 1 else ''}. This separates the traces "
            "from the surrounding copper."
        )
    if kind == "rubout":
        return (
            f"Clears the remaining unwanted copper with a flat end mill, "
            f"{config.rubout_depth:.3f} mm deep, leaving a {config.rubout_clearance:.2f} mm "
            "keep-out around the traces."
        )
    if kind == "cutout":
        if config.tab_enabled and config.tab_count:
            return (
                f"Cuts the board outline free, {config.cutout_depth:.2f} mm deep in "
                f"{config.cutout_stepdown:.2f} mm steps, leaving {config.tab_count} × "
                f"{config.tab_width:.1f} mm break-away tabs. Snap the board out and "
                "file the tabs flat afterwards."
            )
        return (
            f"Cuts the board completely free, {config.cutout_depth:.2f} mm deep in "
            f"{config.cutout_stepdown:.2f} mm steps. Make sure the board is held down "
            "for the final pass."
        )
    return "Runs the toolpaths in this file."
