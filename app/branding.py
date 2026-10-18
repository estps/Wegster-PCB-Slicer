
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap

__all__ = ["draw_logo", "logo_pixmap"]

SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_logo(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    scale = size / 256.0

    def px(value: float) -> float:
        return value * scale

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    cap = Qt.PenCapStyle.RoundCap
    join = Qt.PenJoinStyle.RoundJoin

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#000000"))
    painter.drawRoundedRect(QRectF(px(4), px(4), px(248), px(248)), px(54), px(54))

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor("#242430"), px(3)))
    painter.drawRoundedRect(QRectF(px(4), px(4), px(248), px(248)), px(54), px(54))

    board = QPainterPath()
    board.moveTo(px(40), px(66))
    board.lineTo(px(216), px(66))
    board.lineTo(px(216), px(190))
    board.lineTo(px(40), px(190))
    board.closeSubpath()
    painter.setPen(QPen(QColor("#2C2C3C"), px(6)))
    painter.drawPath(board)

    trace = QPainterPath()
    trace.moveTo(px(62), px(104))
    trace.lineTo(px(92), px(178))
    trace.lineTo(px(128), px(124))
    trace.lineTo(px(164), px(178))
    trace.lineTo(px(194), px(104))
    painter.setPen(QPen(QColor("#FAB387"), px(24), Qt.PenStyle.SolidLine, cap, join))
    painter.drawPath(trace)

    for x, y in ((62, 104), (194, 104)):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#94E2D5"))
        painter.drawEllipse(QPointF(px(x), px(y)), px(14), px(14))
        painter.setBrush(QColor("#000000"))
        painter.drawEllipse(QPointF(px(x), px(y)), px(6), px(6))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#8AB4F8"))
    painter.drawEllipse(QPointF(px(92), px(178)), px(10), px(10))
    painter.drawEllipse(QPointF(px(164), px(178)), px(10), px(10))

    painter.end()
    return image


def logo_pixmap(size: int) -> QPixmap:
    return QPixmap.fromImage(draw_logo(int(size)))
