
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

__all__ = [
    "SHORTCUT_NAME",
    "current_target",
    "install_shortcut",
    "is_current",
    "remove_shortcut",
    "shortcut_path",
    "start_menu_dir",
    "target_key",
]

SHORTCUT_NAME = "Wegstr PCB Slicer.lnk"
DESCRIPTION = "PCB slicer and G-code generator for the Wegstr Light CNC"

_CREATE_NO_WINDOW = 0x08000000


def _is_windows() -> bool:
    return sys.platform == "win32"


def start_menu_dir() -> Path | None:
    if not _is_windows():
        return None
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if candidate.is_dir():
            return candidate
    profile = os.environ.get("USERPROFILE")
    if profile:
        candidate = (
            Path(profile) / "AppData" / "Roaming" / "Microsoft"
            / "Windows" / "Start Menu" / "Programs"
        )
        if candidate.is_dir():
            return candidate
    return None


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def current_target() -> tuple[Path, str, Path, Path | None] | None:
    if not _is_windows():
        return None

    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if not exe.exists():
            return None
        return (exe, "", exe.parent, exe)

    root = project_root()
    runner = Path(sys.executable).with_name("pythonw.exe")
    if not runner.exists():
        runner = Path(sys.executable)
    if not runner.exists():
        return None
    icon = root / "assets" / "wegstr.ico"
    return (runner, "run.py", root, icon if icon.exists() else None)


def shortcut_path(directory: Path | None = None) -> Path | None:
    target_dir = directory or start_menu_dir()
    if target_dir is None:
        return None
    return target_dir / SHORTCUT_NAME


def target_key(
    target: tuple[Path, str, Path, Path | None] | None = None,
) -> str:
    resolved = target or current_target()
    if resolved is None:
        return ""
    executable, arguments, _working_dir, _icon = resolved
    return f"{executable}|{arguments}"


def is_current(
    directory: Path | None = None,
    target: tuple[Path, str, Path, Path | None] | None = None,
    recorded: str = "",
) -> bool:
    path = shortcut_path(directory)
    if path is None or not path.exists():
        return False
    key = target_key(target)
    return bool(key) and key == recorded


def _powershell(script: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_CREATE_NO_WINDOW if _is_windows() else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, (result.stderr or "").strip()


def _quote(value: Path | str) -> str:
    return str(value).replace("'", "''")


def install_shortcut(
    directory: Path | None = None,
    target: tuple[Path, str, Path, Path | None] | None = None,
) -> Path | None:
    resolved = target or current_target()
    if resolved is None:
        return None
    executable, arguments, working_dir, icon = resolved

    path = shortcut_path(directory)
    if path is None:
        return None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    icon_line = (
        f"$link.IconLocation = '{_quote(icon)},0'" if icon is not None else ""
    )
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$shell = New-Object -ComObject WScript.Shell\n"
        f"$link = $shell.CreateShortcut('{_quote(path)}')\n"
        f"$link.TargetPath = '{_quote(executable)}'\n"
        f"$link.Arguments = '{_quote(arguments)}'\n"
        f"$link.WorkingDirectory = '{_quote(working_dir)}'\n"
        f"$link.Description = '{_quote(DESCRIPTION)}'\n"
        f"{icon_line}\n"
        "$link.Save()\n"
    )

    code, error = _powershell(script)
    if code != 0 or not path.exists():
        del error
        return None
    return path


def remove_shortcut(directory: Path | None = None) -> bool:
    path = shortcut_path(directory)
    if path is None or not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True
