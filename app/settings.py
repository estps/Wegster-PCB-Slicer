
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
_KEY_PANEL_HIDDEN = "view/panel_hidden"
_KEY_ARCHIVE = "job/last_archive"
_KEY_DXF_WIDTH = "job/dxf_width"


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
        except Exception:
            pass


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

    def save_dxf_width(self, width: float) -> None:
        try:
            self.settings.setValue(_KEY_DXF_WIDTH, float(width))
            self.settings.sync()
        except Exception:
            pass

    def restore_dxf_width(self) -> float:
        try:
            return float(self.settings.value(_KEY_DXF_WIDTH, 0.2))
        except (TypeError, ValueError):
            return 0.2

    def clear(self) -> None:
        try:
            self.settings.clear()
            self.settings.sync()
        except Exception:
            pass
