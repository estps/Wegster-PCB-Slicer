
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


def main() -> int:
    if "--reset-preferences" in sys.argv:
        from app.settings import Preferences, settings_path

        target = settings_path()
        Preferences().clear()
        print(f"Cleared {target}")
        return 0

    try:
        from app.__main__ import main as run
    except Exception:
        _report(traceback.format_exc())
        raise
    return run()


if __name__ == "__main__":
    sys.exit(main())
