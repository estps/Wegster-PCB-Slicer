
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QSplashScreen

from .branding import logo_pixmap

__all__ = ["build_splash", "MIN_SPLASH_MS"]

MIN_SPLASH_MS = 4000

_WIDTH = 560
_HEIGHT = 330


def build_splash() -> QSplashScreen:
    pixmap = QPixmap(_WIDTH, _HEIGHT)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    card = QRectF(0.5, 0.5, _WIDTH - 1, _HEIGHT - 1)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#000000"))
    painter.drawRoundedRect(card, 22, 22)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QColor("#242430"))
    painter.drawRoundedRect(card, 22, 22)

    logo = logo_pixmap(148)
    painter.drawPixmap(int((_WIDTH - 148) / 2), 34, logo)

    title = QFont()
    title.setPointSize(20)
    title.setWeight(QFont.Weight.DemiBold)
    painter.setFont(title)
    painter.setPen(QColor("#FAB387"))
    painter.drawText(
        QRectF(0, 196, _WIDTH, 34), Qt.AlignmentFlag.AlignHCenter, "Wegstr PCB Slicer"
    )

    body = QFont()
    body.setPointSize(10)
    painter.setFont(body)
    painter.setPen(QColor("#A2A2B4"))
    painter.drawText(
        QRectF(0, 230, _WIDTH, 24),
        Qt.AlignmentFlag.AlignHCenter,
        "Isolation routing for the Wegstr Light CNC",
    )

    bar = QRectF(_WIDTH / 2 - 110, 274, 220, 4)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#161616"))
    painter.drawRoundedRect(bar, 2, 2)
    painter.setBrush(QColor("#8AB4F8"))
    painter.drawRoundedRect(QRectF(bar.left(), bar.top(), 74, 4), 2, 2)

    painter.end()

    splash = QSplashScreen(pixmap)
    splash.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    splash.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    splash.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    return splash
