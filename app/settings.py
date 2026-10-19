
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings

from pcb_engine import SlicerConfig, ToolSpec
from wegstr_gcode import MachineProfile

__all__ = ["Preferences", "settings_path"]

ORG = "Wegstr"
APP = "Wegstr PCB Slicer"

_KEY_GEOMETRY = "window/geometry"
_KEY_MAXIMISED = "window/maximised"
_KEY_CONFIG = "job/config"
_KEY_PROFILE = "job/profile"
_KEY_VIEWER = "viewer"
_KEY_THEME = "view/theme"
_KEY_DENSITY = "view/density"
_KEY_PRESETS = "job/presets"
_KEY_TOOL_PRESETS = "tool/user_presets"
_KEY_PANEL_HIDDEN = "view/panel_hidden"
_KEY_ARCHIVE = "job/last_archive"
_KEY_FILES = "job/last_files"
_KEY_SHORTCUT = "install/start_menu_target"
_KEY_SHORTCUT_VERSION = "install/start_menu_version"


def _settings() -> QSettings:
    return QSettings(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, ORG, APP
    )


def settings_path() -> Path:
    return Path(_settings().fileName())


def _apply(instance: Any, data: dict) -> None:
    for key, value in data.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if isinstance(current, ToolSpec) and isinstance(value, dict):
            _apply(current, value)
        elif isinstance(current, bool):
            setattr(instance, key, bool(value))
        else:
            try:
                setattr(instance, key, value)
            except (TypeError, ValueError):
                pass


class Preferences:

    def __init__(self) -> None:
        self.settings = _settings()


    def save_window(self, window) -> None:
        try:
            self.settings.setValue(_KEY_GEOMETRY, window.saveGeometry())
            self.settings.setValue(_KEY_MAXIMISED, bool(window.isMaximized()))
            self.settings.sync()
        except Exception:
            pass

    def restore_window(self, window) -> bool:
        try:
            geometry = self.settings.value(_KEY_GEOMETRY)
        except Exception:
            return False
        if geometry is None:
            return False

        try:
            window.restoreGeometry(geometry)
            if self.settings.value(_KEY_MAXIMISED, False, type=bool):
                window.showMaximized()
        except Exception:
            return False
        return True


    def save_job(self, config: SlicerConfig, profile: MachineProfile) -> None:
        try:
            self.settings.setValue(_KEY_CONFIG, json.dumps(asdict(config)))
            self.settings.setValue(_KEY_PROFILE, json.dumps(asdict(profile)))
            self.settings.sync()
        except Exception:
            pass

    def restore_job(
        self, config: SlicerConfig, profile: MachineProfile
    ) -> None:
        try:
            raw = self.settings.value(_KEY_CONFIG)
            if raw:
                _apply(config, json.loads(raw))
            raw = self.settings.value(_KEY_PROFILE)
            if raw:
                _apply(profile, json.loads(raw))
            if abs(profile.work_x - 140.0) < 1e-6 and abs(profile.work_y - 90.0) < 1e-6:
                profile.work_y = 200.0
            if abs(profile.rapid_z - 0.6) < 1e-9:
                profile.rapid_z = 2.0
            if abs(config.alignment_hole_diameter - 3.0) < 1e-9:
                config.alignment_hole_diameter = 2.0
        except Exception:
            pass


    def save_theme(self, mode: str) -> None:
        try:
            self.settings.setValue(_KEY_THEME, str(mode))
            self.settings.sync()
        except Exception:
            pass

    def restore_theme(self) -> str:
        try:
            value = self.settings.value(_KEY_THEME, "dark")
            return "light" if str(value).lower() == "light" else "dark"
        except Exception:
            return "dark"

    def save_density(self, value: str) -> None:
        try:
            self.settings.setValue(_KEY_DENSITY, str(value))
            self.settings.sync()
        except Exception:
            pass

    def restore_density(self) -> str:
        try:
            value = self.settings.value(_KEY_DENSITY, "compact")
            return "comfortable" if str(value).lower() == "comfortable" else "compact"
        except Exception:
            return "compact"

    def save_presets(self, presets: dict) -> None:
        try:
            self.settings.setValue(_KEY_PRESETS, json.dumps(presets))
            self.settings.sync()
        except Exception:
            pass

    def restore_presets(self) -> dict:
        try:
            raw = self.settings.value(_KEY_PRESETS)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def save_tool_presets(self, presets: dict) -> None:
        try:
            self.settings.setValue(_KEY_TOOL_PRESETS, json.dumps(presets))
            self.settings.sync()
        except Exception:
            pass

    def restore_tool_presets(self) -> dict:
        try:
            raw = self.settings.value(_KEY_TOOL_PRESETS)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def save_viewer(self, state: dict) -> None:
        try:
            self.settings.setValue(_KEY_VIEWER, json.dumps(state))
            self.settings.sync()
        except Exception:
            pass

    def restore_viewer(self) -> dict:
        try:
            raw = self.settings.value(_KEY_VIEWER)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}


    def save_panel_hidden(self, hidden: bool) -> None:
        try:
            self.settings.setValue(_KEY_PANEL_HIDDEN, bool(hidden))
            self.settings.sync()
        except Exception:
            pass

    def restore_panel_hidden(self) -> bool:
        try:
            return self.settings.value(_KEY_PANEL_HIDDEN, False, type=bool)
        except Exception:
            return False

    def save_archive(self, path: str) -> None:
        try:
            self.settings.setValue(_KEY_ARCHIVE, path)
            self.settings.sync()
        except Exception:
            pass

    def restore_archive(self) -> str | None:
        try:
            value = self.settings.value(_KEY_ARCHIVE)
            return str(value) if value else None
        except Exception:
            return None

    def save_files(self, paths: list[str]) -> None:
        try:
            self.settings.setValue(_KEY_FILES, json.dumps(list(paths)))
            self.settings.sync()
        except Exception:
            pass

    def restore_files(self) -> list[str]:
        try:
            raw = self.settings.value(_KEY_FILES)
            data = json.loads(raw) if raw else []
            return [str(item) for item in data if item]
        except Exception:
            return []

    def shortcut_target(self) -> str:
        try:
            return str(self.settings.value(_KEY_SHORTCUT, "") or "")
        except Exception:
            return ""

    def shortcut_version(self) -> str:
        try:
            return str(self.settings.value(_KEY_SHORTCUT_VERSION, "") or "")
        except Exception:
            return ""

    def set_shortcut_version(self, value: str) -> None:
        try:
            self.settings.setValue(_KEY_SHORTCUT_VERSION, str(value))
            self.settings.sync()
        except Exception:
            pass

    def set_shortcut_target(self, value: str) -> None:
        try:
            self.settings.setValue(_KEY_SHORTCUT, str(value))
            self.settings.sync()
        except Exception:
            pass

    def clear(self) -> None:
        try:
            self.settings.clear()
            self.settings.sync()
        except Exception:
            pass
