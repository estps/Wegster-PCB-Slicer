
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import asdict, dataclass, field
from typing import Iterable, Sequence

from pcb_engine import (
    DrillHit,
    Polyline,
    SlicerConfig,
    ToolpathGroup,
    ToolpathPlan,
)

__all__ = [
    "MachineProfile",
    "WEGSTR_LIGHT",
    "GCodeBuilder",
    "emit_plan",
    "estimate_runtime",
    "GCodeError",
]


class GCodeError(Exception):
    pass


@dataclass
class MachineProfile:

    name: str = "Wegstr Light CNC"

    work_x: float = 140.0
    work_y: float = 90.0
    work_z: float = 40.0

    rapid_z: float = 2.0
    safe_z: float = 8.0
    tool_change_z: float = 20.0

    cut_feed: float = 250.0
    plunge_feed: float = 100.0
    travel_feed: float = 1200.0
    drill_feed: float = 60.0

    spindle_rpm: int = 12000
    spindle_min_rpm: int = 10000
    spindle_max_rpm: int = 15000
    spindle_spinup_dwell: float = 0.5

    units: str = "mm"
    absolute: bool = True
    line_numbers: bool = False
    decimal_places: int = 3
    feed_decimal_places: int = 1
    program_end: str = "M30"
    use_tool_change_pause: bool = True
    return_home_at_end: bool = True

    def clamp_rpm(self, rpm: float) -> int:
        return int(max(self.spindle_min_rpm, min(self.spindle_max_rpm, round(rpm))))

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.rapid_z <= 0:
            problems.append("Rapid height must be above the work surface.")
        if self.safe_z < self.rapid_z:
            problems.append("Safe height must be at or above the rapid height.")
        if self.tool_change_z < self.safe_z:
            problems.append("Tool-change height must be at or above the safe height.")
        if self.plunge_feed <= 0 or self.cut_feed <= 0:
            problems.append("Feed rates must be greater than zero.")
        if self.spindle_min_rpm > self.spindle_max_rpm:
            problems.append("Spindle minimum RPM exceeds the maximum.")
        return problems

    def to_dict(self) -> dict:
        return asdict(self)


WEGSTR_LIGHT = MachineProfile()


class GCodeBuilder:

    def __init__(self, profile: MachineProfile) -> None:
        self.profile = profile
        self.lines: list[str] = []
        self._last_motion: str | None = None
        self._last_x: float | None = None
        self._last_y: float | None = None
        self._last_z: float | None = None
        self._last_feed: float | None = None
        self._line_number = 0
        self._use_line_numbers = profile.line_numbers


    def _coord(self, value: float) -> str:
        places = self.profile.decimal_places
        text = f"{value:.{places}f}"
        if text.startswith("-0.") and float(text) == 0.0:
            text = text[1:]
        return text.rstrip("0").rstrip(".") if "." in text else text

    def _feed(self, value: float) -> str:
        places = self.profile.feed_decimal_places
        return f"{value:.{places}f}".rstrip("0").rstrip(".") or "0"

    @staticmethod
    def _changed(value: float | None, previous: float | None) -> bool:
        if value is None:
            return False
        if previous is None:
            return True
        return abs(value - previous) > 1e-6

    def raw(self, line: str) -> None:
        self.lines.append(line)

    def line(self, body: str) -> None:
        if not body:
            return
        if self._use_line_numbers:
            self._line_number += 1
            self.lines.append(f"N{self._line_number} {body}")
        else:
            self.lines.append(body)

    def comment(self, text: str) -> None:
        for chunk in str(text).splitlines():
            cleaned = chunk.replace("(", "[").replace(")", "]")
            self.line(f"({cleaned})")


    def reset_modal(self) -> None:
        self._last_motion = None
        self._last_x = None
        self._last_y = None
        self._last_z = None
        self._last_feed = None

    def _move(
        self,
        motion: str,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        feed: float | None = None,
    ) -> None:
        parts: list[str] = []
        if motion != self._last_motion:
            parts.append(motion)
            self._last_motion = motion
        if self._changed(x, self._last_x):
            parts.append(f"X{self._coord(x)}")
            self._last_x = x
        if self._changed(y, self._last_y):
            parts.append(f"Y{self._coord(y)}")
            self._last_y = y
        if self._changed(z, self._last_z):
            parts.append(f"Z{self._coord(z)}")
            self._last_z = z
        if self._changed(feed, self._last_feed):
            parts.append(f"F{self._feed(feed)}")
            self._last_feed = feed
        if parts:
            self.line(" ".join(parts))


    def rapid(self, x: float | None = None, y: float | None = None, z: float | None = None) -> None:
        self._move("G0", x, y, z)

    def feed_move(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        feed: float | None = None,
    ) -> None:
        self._move("G1", x, y, z, feed)

    def dwell(self, seconds: float) -> None:
        if seconds > 0:
            self.line(f"G4 P{seconds:.2f}")

    def spindle_on(self, rpm: int) -> None:
        self.line(f"M3 S{self.profile.clamp_rpm(rpm)}")

    def spindle_off(self) -> None:
        self.line("M5")

    def pause(self, message: str = "") -> None:
        if message:
            self.comment(message)
        self.line("M0")

    def program_pause(self) -> None:
        self.line("M0")

    def set_units_and_mode(self) -> None:
        units = "G21" if self.profile.units == "mm" else "G20"
        absolute = "G90" if self.profile.absolute else "G91"
        self.line(units)
        self.line(absolute)
        self.line("G94")
        self.line("G17")
        self.line("G40")
        self.line("G49")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _safe_retract(builder: GCodeBuilder) -> None:
    builder.rapid(z=builder.profile.rapid_z)


def _emit_polyline_at_depth(
    builder: GCodeBuilder,
    polyline: Polyline,
    depth: float,
    profile: MachineProfile,
    cut_feed: float | None = None,
    plunge_feed: float | None = None,
) -> float:
    points = polyline.points
    if len(points) < 2:
        return 0.0

    cut_feed = profile.cut_feed if cut_feed is None else cut_feed
    plunge_feed = profile.plunge_feed if plunge_feed is None else plunge_feed

    _safe_retract(builder)
    builder.rapid(x=float(points[0, 0]), y=float(points[0, 1]))
    builder.feed_move(z=-abs(depth), feed=plunge_feed)

    for index in range(1, len(points)):
        builder.feed_move(
            x=float(points[index, 0]),
            y=float(points[index, 1]),
            feed=cut_feed,
        )

    if polyline.closed:
        builder.feed_move(
            x=float(points[0, 0]),
            y=float(points[0, 1]),
            feed=cut_feed,
        )

    _safe_retract(builder)
    return polyline.length


def _emit_drill(
    builder: GCodeBuilder,
    hole: DrillHit,
    total_depth: float,
    peck_depth: float,
    profile: MachineProfile,
    drill_feed: float | None = None,
) -> None:
    builder.rapid(x=hole.x, y=hole.y)
    builder.rapid(z=profile.rapid_z)

    drill_feed = profile.drill_feed if drill_feed is None else drill_feed
    total = abs(total_depth)
    step = max(peck_depth, 0.05)
    previous = 0.0
    travelled = 0.0
    while travelled < total - 1e-9:
        travelled = min(total, travelled + step)
        target = -travelled
        if target >= previous - 1e-9:
            continue
        builder.feed_move(z=target, feed=drill_feed)
        builder.rapid(z=profile.rapid_z)
        previous = target


def _group_header(
    builder: GCodeBuilder,
    group: ToolpathGroup,
    profile: MachineProfile,
    tool_index: int,
) -> None:
    builder.comment("")
    builder.comment(f"--- Operation {tool_index}: {group.name} ---")
    builder.comment(
        f"Tool: {group.tool.name}  |  depth {group.depth.total_depth:.3f} mm "
        f"in {group.depth.stepdown:.3f} mm steps"
    )
    cut_feed, plunge_feed, rpm = group.tool.resolved_feeds(profile)
    if group.tool.feed_rate or group.tool.plunge_rate or group.tool.spindle_rpm:
        builder.comment(
            f"Tool data: {cut_feed:.0f} mm/min feed, {plunge_feed:.0f} mm/min "
            f"plunge, {rpm} rpm"
        )
    if group.cut_length:
        builder.comment(f"Cutting distance: {group.cut_length:.1f} mm")
    if group.holes:
        builder.comment(f"Holes: {len(group.holes)}")
        deepest = max(
            (h.depth if h.depth is not None else group.depth.total_depth)
            for h in group.holes
        )
        if deepest > group.depth.total_depth + 1e-6:
            pins = sum(1 for h in group.holes if h.registration)
            builder.comment(
                f"NOTE: {pins} alignment pin(s) here go {deepest:.1f} mm deep "
                f"- into the spoilboard"
            )

    if group.requires_flip_before:
        builder.comment("")
        builder.comment("*** FLIP THE WORKPIECE OVER BEFORE CONTINUING ***")
        builder.comment("Remove the board, turn it over, and re-clamp it.")
        builder.pause("Flip the board over, then press cycle start")

    if group.skip_tool_change:
        builder.comment(
            "No tool change: use whatever bit is already in the spindle."
        )
        builder.comment("Whatever bit this is, that is your pin size.")
    elif profile.use_tool_change_pause:
        builder.comment(f"Insert tool: {group.tool.name}")
        builder.pause(f"Change to {group.tool.name}")


def _estimate_seconds(plan: ToolpathPlan, profile: MachineProfile, config: SlicerConfig) -> float:
    seconds = 0.0
    for group in plan.groups:
        cut_feed, plunge_feed, _rpm = group.tool.resolved_feeds(profile)
        passes = len(group.depth.passes())
        cut_length = group.cut_length * passes
        seconds += cut_length / max(cut_feed, 1.0) * 60.0
        plunge_distance = abs(group.depth.total_depth)
        plunge_count = len(group.polylines) * passes
        seconds += plunge_count * (plunge_distance / max(plunge_feed, 1.0) * 60.0)
        seconds += plunge_count * 2.0
        for _hole in group.holes:
            depth = abs(group.depth.total_depth)
            pecks = max(1, int(math.ceil(depth / max(config.peck_depth, 1e-3))))
            seconds += pecks * (depth / max(profile.drill_feed, 1.0) * 60.0)
            seconds += pecks * 1.5
    return seconds


def estimate_runtime(plan: ToolpathPlan, profile: MachineProfile, config: SlicerConfig) -> str:
    seconds = _estimate_seconds(plan, profile, config)
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _margin_text(board: object | None, config: SlicerConfig) -> str:
    offset = getattr(board, "offset", None)
    if offset is None:
        return f"{config.margin:.1f} mm"
    offset_x, offset_y = offset
    if abs(offset_x - offset_y) < 1e-6:
        return f"{offset_x:.1f} mm"
    return f"{offset_x:.1f} x {offset_y:.1f} mm"


def emit_program(
    groups: Sequence[ToolpathGroup],
    config: SlicerConfig,
    profile: MachineProfile,
    program_name: str,
    board: object | None = None,
    preamble: Sequence[str] = (),
    setup_note: str | None = None,
) -> str:
    problems = profile.validate()
    if problems:
        raise GCodeError("; ".join(problems))
    if not groups:
        raise GCodeError("No operations to emit.")

    builder = GCodeBuilder(profile)

    builder.raw("%")
    builder.comment(f"Program   : {program_name}")
    builder.comment(f"Machine   : {profile.name}")
    builder.comment(f"Generated : {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if board is not None:
        builder.comment(
            f"Board     : {board.width:.2f} x {board.height:.2f} mm "
            f"({board.source_size[0]:.2f} x {board.source_size[1]:.2f} mm artwork)"
        )
    builder.comment(
        f"Origin    : {config.origin_mode.replace('_', '-')} + "
        f"{_margin_text(board, config)} margin"
    )
    if setup_note:
        builder.comment(f"Setup     : {setup_note}")
    builder.comment("")

    builder.set_units_and_mode()
    builder.comment(f"Lift to safe height {profile.tool_change_z:.2f} mm before moving")
    builder.rapid(z=profile.tool_change_z)
    builder.rapid(z=profile.rapid_z)

    if preamble:
        builder.comment("")
        for note in preamble:
            builder.comment(f"NOTE: {note}")

    total_cut = 0.0

    for tool_index, group in enumerate(groups, start=1):
        builder.comment("")
        builder.comment("=" * 46)
        _group_header(builder, group, profile, tool_index)

        cut_feed, plunge_feed, rpm = group.tool.resolved_feeds(profile)
        builder.spindle_on(rpm)
        if profile.spindle_spinup_dwell > 0:
            builder.dwell(profile.spindle_spinup_dwell)

        if group.holes:
            for hole in group.holes:
                total = (
                    hole.depth
                    if hole.depth is not None
                    else group.depth.total_depth
                )
                _emit_drill(
                    builder, hole, total, config.peck_depth, profile, plunge_feed
                )
        else:
            passes = group.depth.passes()
            for depth in passes:
                builder.comment(f"Depth {abs(depth):.3f} mm")
                builder.reset_modal()
                for polyline in group.polylines:
                    total_cut += _emit_polyline_at_depth(
                        builder, polyline, depth, profile, cut_feed, plunge_feed
                    )

        builder.spindle_off()
        builder.dwell(0.2)
        _safe_retract(builder)

    builder.comment("")
    builder.comment(f"Total cutting distance: {total_cut:.1f} mm")
    builder.comment("End of program")
    builder.spindle_off()
    if profile.return_home_at_end:
        builder.rapid(z=profile.tool_change_z)
        builder.rapid(x=0.0, y=0.0)
        builder.rapid(z=profile.safe_z)
    builder.line(profile.program_end)
    builder.raw("%")

    return builder.text()


def emit_plan(
    plan: ToolpathPlan,
    config: SlicerConfig,
    profile: MachineProfile | None = None,
    program_name: str = "pcb",
) -> str:
    if not plan.groups:
        raise GCodeError("The plan contains no operations to emit.")
    return emit_program(
        plan.groups,
        config,
        profile or WEGSTR_LIGHT,
        program_name,
        board=plan.board,
        preamble=plan.warnings,
    )
