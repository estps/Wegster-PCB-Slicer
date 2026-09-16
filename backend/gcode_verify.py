
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from wegstr_gcode import MachineProfile

__all__ = ["Issue", "VerificationReport", "verify_program", "verify_files"]

_WORD = re.compile(r"([A-Za-z])\s*([+-]?[0-9]*\.?[0-9]+)")

IN_MATERIAL_Z = -0.001


@dataclass
class Issue:
    severity: str
    line: int
    message: str

    def to_dict(self) -> dict:
        return {"severity": self.severity, "line": self.line, "message": self.message}


@dataclass
class VerificationReport:
    ok: bool
    issues: list[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    source: str = ""

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def headline(self) -> str:
        if self.errors:
            return f"{len(self.errors)} error(s)"
        if self.warnings:
            return f"OK with {len(self.warnings)} warning(s)"
        return "All checks passed"

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "headline": self.headline(),
            "issues": [i.to_dict() for i in self.issues],
            "stats": self.stats,
        }


class _Interpreter:

    def __init__(self, profile: MachineProfile, max_depth: float | None) -> None:
        self.profile = profile
        self.max_depth = max_depth

        self.units_mm = False
        self.absolute = False
        self.motion = 1
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.feed: float | None = None
        self.spindle = False

        self.issues: list[Issue] = []
        self.markers: list[int] = []
        self.program_end_seen = False
        self.g91_seen = False

        self.rapid_moves = 0
        self.cut_moves = 0
        self.cut_distance = 0.0
        self.rapid_distance = 0.0
        self.seconds = 0.0
        self.feed_rates: set[float] = set()
        self.tool_changes = 0
        self.rpm: int | None = None

        self.xy_bounds: list[float] | None = None
        self.cut_bounds: list[float] | None = None
        self.z_min = 0.0
        self.z_max = 0.0
        self.spindle_ever_on = False

    def error(self, line: int, message: str) -> None:
        self.issues.append(Issue("error", line, message))

    def warn(self, line: int, message: str) -> None:
        self.issues.append(Issue("warning", line, message))

    def _extend(self, bounds: list[float] | None, x: float, y: float) -> list[float]:
        if bounds is None:
            return [x, y, x, y]
        bounds[0] = min(bounds[0], x)
        bounds[1] = min(bounds[1], y)
        bounds[2] = max(bounds[2], x)
        bounds[3] = max(bounds[3], y)
        return bounds

    def run(self, text: str) -> None:
        lines = text.splitlines()
        for number, raw in enumerate(lines, start=1):
            line = raw.split("(", 1)[0].strip()
            if not line:
                continue
            if line == "%":
                self.markers.append(number)
                continue
            if line.startswith("("):
                continue
            self._line(number, line)

        self._finish(len(lines))

    def _line(self, number: int, line: str) -> None:
        words = _WORD.findall(line)
        if not words:
            return

        for letter, value in words:
            letter = letter.upper()
            if letter != "G":
                continue
            try:
                code = int(float(value))
            except ValueError:
                continue
            if code == 20:
                self.error(number, "G20 selects inches; the Wegstr profile is metric")
            elif code == 21:
                self.units_mm = True
            elif code == 90:
                self.absolute = True
            elif code == 91:
                self.absolute = False
                self.g91_seen = True
            elif code in (0, 1, 2, 3):
                self.motion = code
            elif code == 4:
                pass

        target_x = self.x
        target_y = self.y
        target_z = self.z
        saw_xy = False
        saw_z = False
        dwell: float | None = None

        for letter, value in words:
            letter = letter.upper()
            try:
                number_value = float(value)
            except ValueError:
                continue
            if letter == "X":
                target_x = number_value if self.absolute else self.x + number_value
                saw_xy = True
            elif letter == "Y":
                target_y = number_value if self.absolute else self.y + number_value
                saw_xy = True
            elif letter == "Z":
                target_z = number_value if self.absolute else self.z + number_value
                saw_z = True
            elif letter == "F":
                self.feed = number_value
                self.feed_rates.add(number_value)
            elif letter == "S":
                self.rpm = int(number_value)
            elif letter == "P":
                dwell = number_value
            elif letter == "M":
                self._m_code(number, int(number_value))

        if not saw_xy and not saw_z:
            if dwell:
                self.seconds += dwell
            return

        distance_xy = ((target_x - self.x) ** 2 + (target_y - self.y) ** 2) ** 0.5
        distance_z = abs(target_z - self.z)
        distance = (distance_xy**2 + distance_z**2) ** 0.5

        if self.motion == 0:
            self._rapid(number, saw_xy, saw_z, distance_xy, distance)
        else:
            self._cut(number, saw_xy, saw_z, distance_xy, distance)

        self.x, self.y, self.z = target_x, target_y, target_z
        self.z_min = min(self.z_min, self.z)
        self.z_max = max(self.z_max, self.z)

        if saw_xy:
            self.xy_bounds = self._extend(self.xy_bounds, self.x, self.y)
            if self.motion == 1 and self.z <= IN_MATERIAL_Z:
                self.cut_bounds = self._extend(self.cut_bounds, self.x, self.y)

    def _m_code(self, number: int, code: int) -> None:
        if code == 3:
            self.spindle = True
            self.spindle_ever_on = True
        elif code in (4, 5):
            if code == 5:
                self.spindle = False
        elif code == 0:
            self.tool_changes += 1
        elif code in (2, 30):
            self.program_end_seen = True

    def _rapid(
        self,
        number: int,
        saw_xy: bool,
        saw_z: bool,
        distance_xy: float,
        distance: float,
    ) -> None:
        self.rapid_moves += 1
        self.rapid_distance += distance
        if saw_xy and self.z < self.profile.rapid_z - 0.05:
            self.error(
                number,
                f"rapid XY move while the tool is down at Z{self.z:.3f} "
                f"(should retract to Z{self.profile.rapid_z:.2f} first)",
            )
        feed = self.profile.travel_feed
        if feed > 0:
            self.seconds += distance / feed * 60.0
        del saw_z, distance_xy

    def _cut(
        self,
        number: int,
        saw_xy: bool,
        saw_z: bool,
        distance_xy: float,
        distance: float,
    ) -> None:
        self.cut_moves += 1
        self.cut_distance += distance
        if not self.spindle:
            self.error(number, "cutting move with the spindle stopped")
        if self.feed is None:
            self.error(number, "cutting move before any feed rate (F) was set")
        if saw_xy and not saw_z and self.z > IN_MATERIAL_Z:
            self.warn(number, f"G1 traverse above the work surface at Z{self.z:.2f}")
        feed = self.feed or 0.0
        if feed > 0:
            self.seconds += distance / feed * 60.0
        if saw_z and self.max_depth is not None and self.z < -abs(self.max_depth) - 1e-6:
            self.error(
                number,
                f"plunge to Z{self.z:.3f} exceeds the configured depth "
                f"of {abs(self.max_depth):.3f} mm",
            )

    def _finish(self, last_line: int) -> None:
        profile = self.profile

        if not self.markers:
            self.error(0, "program is not delimited with % markers")
        else:
            if self.markers[0] != 1 and self.markers[0] > 5:
                self.warn(self.markers[0], "leading % marker is not at the top")
            if len(self.markers) < 2:
                self.warn(self.markers[-1], "program has no closing % marker")

        if not self.units_mm:
            self.error(0, "G21 (millimetres) is never selected")
        if not self.absolute:
            self.error(0, "G90 (absolute positioning) is never selected")
        if self.g91_seen:
            self.error(0, "program contains G91 incremental moves")

        if not self.program_end_seen:
            self.error(last_line, f"program never reaches {profile.program_end}")

        if self.spindle:
            self.warn(last_line, "spindle is still running at the end of the program")

        if self.xy_bounds is not None:
            min_x, min_y, max_x, max_y = self.xy_bounds
            if min_x < -0.001 or min_y < -0.001:
                self.error(
                    0,
                    f"toolpath goes negative (X{min_x:.3f}, Y{min_y:.3f}); "
                    "check the work origin",
                )
            if max_x > profile.work_x + 0.001:
                self.error(
                    0,
                    f"toolpath reaches X{max_x:.3f} which exceeds the "
                    f"{profile.work_x:.0f} mm work area",
                )
            if max_y > profile.work_y + 0.001:
                self.error(
                    0,
                    f"toolpath reaches Y{max_y:.3f} which exceeds the "
                    f"{profile.work_y:.0f} mm work area",
                )

        if self.z_min < -profile.work_z:
            self.error(0, f"Z{self.z_min:.3f} exceeds the {profile.work_z:.0f} mm Z travel")
        if self.z_max > profile.tool_change_z + 0.001:
            self.error(
                0,
                f"Z{self.z_max:.3f} exceeds the tool-change height "
                f"of {profile.tool_change_z:.2f} mm",
            )

        if self.cut_moves == 0:
            self.warn(0, "program contains no cutting moves")

        self.stats = {
            "lines": last_line,
            "rapid_moves": self.rapid_moves,
            "cut_moves": self.cut_moves,
            "rapid_distance": round(self.rapid_distance, 2),
            "cut_distance": round(self.cut_distance, 2),
            "xy_bounds": [round(v, 3) for v in self.xy_bounds] if self.xy_bounds else None,
            "cut_bounds": [round(v, 3) for v in self.cut_bounds] if self.cut_bounds else None,
            "z_min": round(self.z_min, 4),
            "z_max": round(self.z_max, 4),
            "feed_rates": sorted(self.feed_rates),
            "spindle_rpm": self.rpm,
            "tool_changes": self.tool_changes,
            "estimated_minutes": round(self.seconds / 60.0, 1),
        }


def verify_program(
    text: str,
    profile: MachineProfile,
    max_cut_depth: float | None = None,
    source: str = "",
) -> VerificationReport:
    interpreter = _Interpreter(profile, max_cut_depth)
    interpreter.run(text)
    report = VerificationReport(
        ok=not interpreter.issues or not any(
            i.severity == "error" for i in interpreter.issues
        ),
        issues=interpreter.issues,
        stats=interpreter.stats,
        source=source,
    )
    return report


def verify_files(
    programs: Sequence[tuple[str, str]],
    profile: MachineProfile,
    max_cut_depth: float | None = None,
) -> VerificationReport:
    merged = VerificationReport(ok=True, source="package")
    total_lines = 0
    total_cut = 0.0
    total_rapid = 0.0
    total_seconds = 0.0
    bounds: list[float] | None = None
    z_min = 0.0

    for name, text in programs:
        report = verify_program(text, profile, max_cut_depth, source=name)
        for issue in report.issues:
            merged.issues.append(
                Issue(issue.severity, issue.line, f"{name}: {issue.message}")
            )
        stats = report.stats
        total_lines += stats.get("lines", 0)
        total_cut += stats.get("cut_distance", 0.0)
        total_rapid += stats.get("rapid_distance", 0.0)
        total_seconds += stats.get("estimated_minutes", 0.0) * 60.0
        z_min = min(z_min, stats.get("z_min", 0.0))
        file_bounds = stats.get("xy_bounds")
        if file_bounds:
            if bounds is None:
                bounds = list(file_bounds)
            else:
                bounds[0] = min(bounds[0], file_bounds[0])
                bounds[1] = min(bounds[1], file_bounds[1])
                bounds[2] = max(bounds[2], file_bounds[2])
                bounds[3] = max(bounds[3], file_bounds[3])

    merged.ok = not any(i.severity == "error" for i in merged.issues)
    merged.stats = {
        "files": len(programs),
        "lines": total_lines,
        "cut_distance": round(total_cut, 2),
        "rapid_distance": round(total_rapid, 2),
        "xy_bounds": [round(v, 3) for v in bounds] if bounds else None,
        "z_min": round(z_min, 4),
        "estimated_minutes": round(total_seconds / 60.0, 1),
    }
    return merged
