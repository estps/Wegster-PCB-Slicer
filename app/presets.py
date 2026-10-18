
from __future__ import annotations

from dataclasses import fields
from typing import Any

from pcb_engine import SlicerConfig, ToolSpec

__all__ = [
    "BUILTIN",
    "apply_preset",
    "capture",
    "coerce",
    "preset_names",
]

_SKIP = ("machine_x", "machine_y", "machine_z")

BUILTIN: dict[str, dict[str, Any]] = {
    "Speed": {
        "isolation_passes": 1,
        "isolation_offsets": 1,
        "rubout_enabled": False,
        "silkscreen_enabled": False,
        "cutout_stepdown": 1.0,
        "peck_depth": 1.5,
        "tab_count": 4,
    },
    "Balanced": {
        "isolation_passes": 1,
        "isolation_offsets": 1,
        "rubout_enabled": True,
        "silkscreen_enabled": True,
        "cutout_stepdown": 0.5,
        "peck_depth": 0.8,
        "tab_count": 4,
    },
    "Precise": {
        "isolation_passes": 3,
        "isolation_offsets": 2,
        "rubout_enabled": True,
        "silkscreen_enabled": True,
        "cutout_stepdown": 0.25,
        "peck_depth": 0.4,
        "tab_count": 4,
    },
}


def preset_names(user: dict[str, Any]) -> list[str]:
    return list(BUILTIN) + [name for name in user if name not in BUILTIN]


def coerce(value: Any, current: Any) -> Any:
    if isinstance(current, ToolSpec):
        if isinstance(value, ToolSpec):
            return value
        if isinstance(value, dict):
            merged = current.to_dict()
            merged.update(value)
            try:
                return ToolSpec(**merged)
            except TypeError:
                return current
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


def apply_preset(config: SlicerConfig, data: dict[str, Any]) -> list[str]:
    applied: list[str] = []
    known = {info.name for info in fields(SlicerConfig)}
    for key, value in data.items():
        if key not in known or key in _SKIP:
            continue
        current = getattr(config, key)
        try:
            setattr(config, key, coerce(value, current))
        except (TypeError, ValueError):
            continue
        applied.append(key)
    return applied


def capture(config: SlicerConfig) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for info in fields(SlicerConfig):
        if info.name in _SKIP:
            continue
        value = getattr(config, info.name)
        data[info.name] = value.to_dict() if isinstance(value, ToolSpec) else value
    return data
