
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from . import theme
from .window import MainWindow


def main(argv: list[str] | None = None) -> int:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Wegstr PCB Slicer")
    app.setOrganizationName("Wegstr")
    theme.apply(app)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
