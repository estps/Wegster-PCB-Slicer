
from __future__ import annotations

import sys
import traceback


def _report(message: str) -> None:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Wegstr PCB Slicer")
        box.setText("The slicer could not start.")
        box.setDetailedText(message)
        box.exec()
        del app
    except Exception:
        pass


def self_test(argv: list[str]) -> int:
    import datetime
    from pathlib import Path

    try:
        from app import BACKEND_DIR

        del BACKEND_DIR
    except Exception:
        pass

    lines: list[str] = []

    def note(text: str) -> None:
        lines.append(text)

    note(f"Wegstr PCB Slicer self-test  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    note(f"frozen : {getattr(sys, 'frozen', False)}")
    note(f"python : {sys.version.split()[0]}")

    ok = True

    try:
        from main import VERSION

        note(f"version: {VERSION}")
    except Exception as exc:
        ok = False
        note(f"version: FAILED ({exc})")

    try:
        from tool_db import load_tool_db

        database = load_tool_db()
        note(f"tool db: {len(database)} tools from {database.path.name}")
    except Exception as exc:
        note(f"tool db: unavailable ({type(exc).__name__}: {exc})")

    try:
        from sqlite_read import SqliteReader  # noqa: F401

        note("sqlite reader: present")
    except Exception as exc:
        ok = False
        note(f"sqlite reader: FAILED ({exc})")

    try:
        from updater import current_version, parse_version

        note(f"updater: present, parses {parse_version('v1.2.3')}, current {current_version()}")
    except Exception as exc:
        ok = False
        note(f"updater: FAILED ({exc})")

    archive = None
    for candidate in argv:
        if candidate.lower().endswith(".zip"):
            archive = Path(candidate)
            break

    if archive is None or not archive.exists():
        try:
            from main import DEFAULT_ARCHIVE

            if DEFAULT_ARCHIVE.exists():
                archive = DEFAULT_ARCHIVE
        except Exception:
            archive = None

    if archive is None:
        note("pipeline: skipped (no Gerber archive given)")
    else:
        note(f"archive: {archive}")
        try:
            from gerber_io import load_project
            from pcb_engine import SlicerConfig, plan_toolpaths
            from wegstr_gcode import emit_plan, estimate_runtime

            project = load_project(archive)
            config = SlicerConfig()
            plan = plan_toolpaths(project, config)
            program = emit_plan(plan, config, program_name="self-test")
            note(
                f"pipeline: {len(plan.groups)} operations, "
                f"{plan.total_cut_length:.0f} mm of cutting, "
                f"{program.count(chr(10))} G-code lines, "
                f"{len(project.all_holes)} holes"
            )
            project.cleanup()
        except Exception as exc:
            ok = False
            note(f"pipeline: FAILED ({type(exc).__name__}: {exc})")

    note(f"result : {'PASS' if ok else 'FAIL'}")

    target = Path("wegstr-self-test.txt")
    for index, value in enumerate(argv):
        if value == "--report" and index + 1 < len(argv):
            target = Path(argv[index + 1])
    try:
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass

    print("\n".join(lines))
    return 0 if ok else 1


def main() -> int:
    if "--reset-preferences" in sys.argv:
        from app.settings import Preferences, settings_path

        target = settings_path()
        Preferences().clear()
        print(f"Cleared {target}")
        return 0

    if "--self-test" in sys.argv:
        return self_test(sys.argv[1:])

    try:
        from app.__main__ import main as run
    except Exception:
        _report(traceback.format_exc())
        raise
    return run()


if __name__ == "__main__":
    sys.exit(main())
