
from __future__ import annotations

import sys
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QSlider,
    QSpinBox,
)

from . import theme
from .splash import MIN_SPLASH_MS, build_splash
from .window import MainWindow

_SCROLL_SENSITIVE = (QSlider, QDoubleSpinBox, QSpinBox, QComboBox)


class WheelGuard(QObject):

    def eventFilter(self, watched, event) -> bool:
        if event.type() != QEvent.Type.Wheel:
            return False
        if not isinstance(watched, _SCROLL_SENSITIVE):
            return False
        if watched.hasFocus():
            return False

        parent = watched.parent()
        while parent is not None and not isinstance(parent, QAbstractScrollArea):
            parent = parent.parent()
        if isinstance(parent, QAbstractScrollArea):
            bar = parent.verticalScrollBar()
            bar.setValue(bar.value() - event.angleDelta().y())
        event.ignore()
        return True


def main(argv: list[str] | None = None) -> int:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Wegstr PCB Slicer")
    app.setOrganizationName("Wegstr")
    theme.apply(app)
    app._wheel_guard = WheelGuard(app)
    app.installEventFilter(app._wheel_guard)

    splash = build_splash()
    splash.show()
    app.processEvents()
    started = time.monotonic()

    window = MainWindow()

    def reveal() -> None:
        window.show()
        splash.finish(window)

    elapsed_ms = (time.monotonic() - started) * 1000.0
    delay = max(0, int(MIN_SPLASH_MS - elapsed_ms))
    QTimer.singleShot(delay, reveal)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
