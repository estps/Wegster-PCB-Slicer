
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MoveSegment", "GCodeProgram", "read_program", "read_file"]

_WORD = re.compile(r"([A-Za-z])\s*([+-]?[0-9]*\.?[0-9]+)")

IN_MATERIAL_Z = -0.001


@dataclass
class MoveSegment:

    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float
    rapid: bool
    index: int
    line: int

    @property
    def cutting(self) -> bool:
        return not self.rapid and self.z1 <= IN_MATERIAL_Z

    @property
    def length(self) -> float:
        return (
            (self.x1 - self.x0) ** 2
            + (self.y1 - self.y0) ** 2
            + (self.z1 - self.z0) ** 2
        ) ** 0.5


@dataclass
class GCodeProgram:

    name: str = ""
    path: Path | None = None
    segments: list[MoveSegment] = field(default_factory=list)
    pauses: list[tuple[float, float, int]] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def max_index(self) -> int:
        return len(self.segments)

    @property
    def bounds(self) -> tuple[float, float, float, float] | None:
        if not self.segments:
            return None
        xs = [s.x0 for s in self.segments] + [s.x1 for s in self.segments]
        ys = [s.y0 for s in self.segments] + [s.y1 for s in self.segments]
        return (min(xs), min(ys), max(xs), max(ys))

    def depths(self) -> list[float]:
        return sorted({round(s.z1, 3) for s in self.segments if s.cutting})


def read_program(text: str, name: str = "", path: Path | None = None) -> GCodeProgram:
    program = GCodeProgram(name=name, path=path)

    x = y = z = 0.0
    absolute = True
    motion = 0
    spindle = False
    feed: float | None = None
    rpm: int | None = None
    tool_changes = 0
    seconds = 0.0
    cut_distance = 0.0
    plunge_distance = 0.0
    rapid_distance = 0.0
    line_count = 0
    index = 0
    z_min = 0.0
    z_max = 0.0

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line_count = lineno
        line = raw.split("(", 1)[0].strip()
        if not line or line == "%":
            continue

        words = _WORD.findall(line)
        if not words:
            continue

        for letter, value in words:
            if letter.upper() != "G":
                continue
            try:
                code = int(float(value))
            except ValueError:
                continue
            if code == 90:
                absolute = True
            elif code == 91:
                absolute = False
            elif code in (0, 1, 2, 3):
                motion = code

        target_x, target_y, target_z = x, y, z
        saw_axis = False
        dwell: float | None = None

        for letter, value in words:
            letter = letter.upper()
            try:
                number = float(value)
            except ValueError:
                continue
            if letter == "X":
                target_x = number if absolute else x + number
                saw_axis = True
            elif letter == "Y":
                target_y = number if absolute else y + number
                saw_axis = True
            elif letter == "Z":
                target_z = number if absolute else z + number
                saw_axis = True
            elif letter == "F":
                feed = number
            elif letter == "S":
                rpm = int(number)
            elif letter == "P":
                dwell = number
            elif letter == "M":
                code = int(number)
                if code == 3:
                    spindle = True
                elif code == 5:
                    spindle = False
                elif code in (0, 1):
                    tool_changes += 1
                    program.pauses.append((x, y, index))

        if not saw_axis:
            if dwell:
                seconds += dwell
            continue

        segment = MoveSegment(
            x0=x,
            y0=y,
            z0=z,
            x1=target_x,
            y1=target_y,
            z1=target_z,
            rapid=motion == 0,
            index=index,
            line=lineno,
        )
        program.segments.append(segment)
        index += 1

        length = segment.length
        lateral = (target_x - x) ** 2 + (target_y - y) ** 2 > 1e-18
        if segment.rapid:
            rapid_distance += length
            seconds += length / 1200.0 * 60.0
        else:
            if lateral:
                cut_distance += length
            else:
                plunge_distance += length
            speed = feed or 100.0
            seconds += length / speed * 60.0

        x, y, z = target_x, target_y, target_z
        z_min = min(z_min, z)
        z_max = max(z_max, z)

    bounds = program.bounds
    program.stats = {
        "name": name,
        "lines": line_count,
        "segments": len(program.segments),
        "cut_distance": round(cut_distance, 2),
        "plunge_distance": round(plunge_distance, 2),
        "rapid_distance": round(rapid_distance, 2),
        "z_min": round(z_min, 4),
        "z_max": round(z_max, 4),
        "depths": program.depths(),
        "bounds": [round(v, 3) for v in bounds] if bounds else None,
        "tool_changes": tool_changes,
        "spindle_rpm": rpm,
        "estimated_minutes": round(seconds / 60.0, 1),
        "spindle_ever_on": spindle,
    }
    return program


def read_file(path: str | Path) -> GCodeProgram:
    resolved = Path(path).expanduser()
    text = resolved.read_text(encoding="utf-8", errors="replace")
    return read_program(text, name=resolved.name, path=resolved)
