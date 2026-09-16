
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget

from . import theme

SETTLE_MS = 90

MARGIN = 0.25

MAX_CACHE_PIXELS = 24_000_000


@dataclass
class PathLayer:

    kind: str
    colour: str
    width: float
    polygons: list[QPolygonF] = field(default_factory=list)
    visible: bool = True
    closed: bool = False


class ToolpathView(QGraphicsView):

    cursorMoved = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._scene = QGraphicsScene(self)
        self._scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.setScene(self._scene)
        self.setSceneRect(QRectF(0.0, 0.0, 140.0, 90.0))

        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontSavePainterState, True
        )
        self.setBackgroundBrush(theme.qcolor(theme.CRUST))
        self.viewport().setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

        self._machine = (140.0, 90.0)
        self._bed = QRectF(0.0, 0.0, 140.0, 90.0)
        self._outline: QPolygonF | None = None
        self._copper: list[tuple[str, list[QPolygonF]]] = []
        self._layers: list[PathLayer] = []
        self._holes: list[tuple[float, float, float]] = []
        self._show_copper = True
        self._show_outline = True
        self._show_bed = True
        self._show_holes = True
        self._layer_visible: dict[str, bool] = {}
        self.show_legend = False

        self._program = None
        self._program_buckets: list[tuple[float, str, list[QLineF], np.ndarray]] = []
        self._program_plunges: list[
            tuple[float, str, list[QPointF], np.ndarray]
        ] = []
        self._program_rapids: tuple[list[QLineF], np.ndarray] | None = None
        self._program_pauses: list[tuple[float, float, int]] = []
        self._progress = 1.0
        self._progress_index = 0
        self.show_rapids = True
        self.show_program_pauses = True

        self._cache: QPixmap | None = None
        self._cache_rect = QRectF()
        self._cache_scale = 0.0
        self._cache_progress = -1

        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._rebuild_cache)

        self._panning = False
        self._pan_origin = QPointF()
        self._cursor = QPointF()
        self._has_cursor = False

        self._min_scale = 0.05
        self._max_scale = 400.0


    def clear(self) -> None:
        self._outline = None
        self._copper.clear()
        self._layers.clear()
        self._holes.clear()
        self.clear_program()
        self._invalidate()
        self.viewport().update()

    def _invalidate(self) -> None:
        self._cache = None
        self._cache_rect = QRectF()
        self._cache_progress = -1


    def clear_program(self) -> None:
        self._program = None
        self._program_buckets = []
        self._program_plunges = []
        self._program_rapids = None
        self._program_pauses = []
        self._progress = 1.0
        self._progress_index = 0

    def set_program(self, program, machine: tuple[float, float]) -> None:
        self.set_machine(machine)
        flip = machine[1]

        by_depth: dict[float, list[tuple[int, QLineF]]] = {}
        plunges_by_depth: dict[float, list[tuple[int, QPointF]]] = {}
        rapid_lines: list[QLineF] = []
        rapid_indices: list[int] = []

        for segment in program.segments:
            lateral = (
                abs(segment.x1 - segment.x0) > 1e-9
                or abs(segment.y1 - segment.y0) > 1e-9
            )
            if not lateral:
                if not segment.rapid and segment.cutting:
                    plunges_by_depth.setdefault(round(segment.z1, 3), []).append(
                        (segment.index, QPointF(segment.x1, flip - segment.y1))
                    )
                continue
            line = QLineF(
                segment.x0, flip - segment.y0, segment.x1, flip - segment.y1
            )
            if segment.rapid:
                rapid_lines.append(line)
                rapid_indices.append(segment.index)
            elif segment.cutting:
                by_depth.setdefault(round(segment.z1, 3), []).append(
                    (segment.index, line)
                )

        depths = sorted(set(by_depth) | set(plunges_by_depth))
        deepest = depths[0] if depths else 0.0
        shallowest = depths[-1] if depths else 0.0
        self._program_buckets = [
            (
                depth,
                _depth_colour(depth, deepest, shallowest),
                [line for _, line in by_depth[depth]],
                np.asarray([i for i, _ in by_depth[depth]], dtype=np.int64),
            )
            for depth in sorted(by_depth)
        ]
        self._program_plunges = [
            (
                depth,
                _depth_colour(depth, deepest, shallowest),
                [point for _, point in plunges_by_depth[depth]],
                np.asarray(
                    [i for i, _ in plunges_by_depth[depth]], dtype=np.int64
                ),
            )
            for depth in sorted(plunges_by_depth)
        ]
        self._program_rapids = (
            (rapid_lines, np.asarray(rapid_indices, dtype=np.int64))
            if rapid_lines
            else None
        )
        self._program_pauses = [
            (x, flip - y, index) for x, y, index in program.pauses
        ]
        self._program = program

        self._progress = 1.0
        self._progress_index = program.max_index
        self._invalidate()
        self._rebuild_cache()
        self.viewport().update()

    def set_progress(self, fraction: float) -> None:
        self._progress = max(0.0, min(1.0, fraction))
        self._progress_index = (
            int(round(self._progress * self._program.max_index))
            if self._program is not None
            else 0
        )
        self._rebuild_cache()
        self.viewport().update()

    def has_program(self) -> bool:
        return self._program is not None

    def refresh(self) -> None:
        self._invalidate()
        self._rebuild_cache()
        self.viewport().update()

    def set_machine(self, machine: tuple[float, float]) -> None:
        self._machine = machine
        self._bed = QRectF(0.0, 0.0, machine[0], machine[1])
        self.setSceneRect(self._bed)
        self.fit()

    def set_plan(self, plan, machine: tuple[float, float]) -> None:
        self.set_machine(machine)
        flip = machine[1]

        def to_polygon(points) -> QPolygonF:
            return QPolygonF([QPointF(float(x), flip - float(y)) for x, y in points])

        board = plan.board
        outline_coords = list(board.outline.exterior.coords)
        self._outline = to_polygon(outline_coords) if len(outline_coords) > 2 else None

        self._copper = []
        for side, rings in board.copper_preview().items():
            colour = theme.COPPER_BOTTOM if side == "bottom" else theme.COPPER_TOP
            self._copper.append((colour, [to_polygon(r) for r in rings if len(r) > 2]))

        self._layers = []
        self._holes = []
        for group in plan.groups:
            layer = self._layer_for(group.kind)
            if layer is not None:
                for polyline in group.polylines:
                    points = polyline.points
                    if len(points) < 2:
                        continue
                    polygon = to_polygon(points)
                    if polyline.closed and polygon.count() > 0:
                        polygon.append(polygon.at(0))
                    layer.polygons.append(polygon)
            for hole in group.holes:
                self._holes.append(
                    (hole.x, flip - hole.y, max(hole.diameter / 2.0, 0.05))
                )

        self._settle.stop()
        self._rebuild_cache()
        self.viewport().update()

    def _layer_for(self, kind: str) -> PathLayer | None:
        for layer in self._layers:
            if layer.kind == kind:
                return layer
        spec = {
            "isolation": (theme.ISOLATION, 0.9),
            "rubout": (theme.RUBOUT, 0.9),
            "cutout": (theme.CUTOUT, 1.6),
        }.get(kind)
        if spec is None:
            return None
        layer = PathLayer(
            kind=kind,
            colour=spec[0],
            width=spec[1],
            visible=self._layer_visible.get(kind, True),
        )
        self._layers.append(layer)
        return layer


    def set_visible(self, kind: str, visible: bool) -> None:
        self._layer_visible[kind] = bool(visible)
        for layer in self._layers:
            if layer.kind == kind:
                layer.visible = bool(visible)
        self.viewport().update()


    def visibility(self) -> dict:
        return {
            "copper": self._show_copper,
            "outline": self._show_outline,
            "bed": self._show_bed,
            "holes": self._show_holes,
            "legend": self.show_legend,
            "rapids": self.show_rapids,
            "pauses": self.show_program_pauses,
            "layers": dict(self._layer_visible),
        }

    def apply_visibility(self, state: dict) -> None:
        if not state:
            return
        self._show_copper = bool(state.get("copper", self._show_copper))
        self._show_outline = bool(state.get("outline", self._show_outline))
        self._show_bed = bool(state.get("bed", self._show_bed))
        self._show_holes = bool(state.get("holes", self._show_holes))
        self.show_legend = bool(state.get("legend", self.show_legend))
        self.show_rapids = bool(state.get("rapids", self.show_rapids))
        self.show_program_pauses = bool(
            state.get("pauses", self.show_program_pauses)
        )
        for kind, visible in (state.get("layers") or {}).items():
            self._layer_visible[str(kind)] = bool(visible)
        for layer in self._layers:
            layer.visible = self._layer_visible.get(layer.kind, layer.visible)
        self.refresh()

    def set_copper_visible(self, visible: bool) -> None:
        self._show_copper = visible
        self.viewport().update()

    def set_outline_visible(self, visible: bool) -> None:
        self._show_outline = visible
        self.viewport().update()

    def set_holes_visible(self, visible: bool) -> None:
        self._show_holes = visible
        self.viewport().update()

    def set_bed_visible(self, visible: bool) -> None:
        self._show_bed = visible
        self.viewport().update()


    def fit(self) -> None:
        rect = self.sceneRect()
        if rect.isEmpty():
            return
        self.resetTransform()
        self.fitInView(rect.adjusted(-3.0, -3.0, 3.0, 3.0), Qt.AspectRatioMode.KeepAspectRatio)
        base = self.transform().m11()
        self._min_scale = base * 0.6
        self._max_scale = base * 500.0
        self._schedule_rebuild()

    def zoom_by(self, factor: float) -> None:
        current = self.transform().m11()
        if current <= 0.0:
            return
        target = current * factor
        if target < self._min_scale:
            factor = self._min_scale / current
        elif target > self._max_scale:
            factor = self._max_scale / current
        if abs(factor - 1.0) < 1e-6:
            return
        self.scale(factor, factor)
        self._schedule_rebuild()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta:
            self.zoom_by(1.0018 ** delta)
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._panning = True
            self._pan_origin = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning:
            delta = event.position() - self._pan_origin
            self._pan_origin = event.position()
            horizontal = self.horizontalScrollBar()
            vertical = self.verticalScrollBar()
            horizontal.setValue(horizontal.value() - int(delta.x()))
            vertical.setValue(vertical.value() - int(delta.y()))

        self._cursor = event.position()
        self._has_cursor = True
        scene_point = self.mapToScene(event.position().toPoint())
        self.cursorMoved.emit(scene_point.x(), self._machine[1] - scene_point.y())
        self.viewport().update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._panning = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        self._has_cursor = False
        self.viewport().update()
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_rebuild()


    def _schedule_rebuild(self) -> None:
        self._settle.start()

    def _rebuild_cache(self) -> None:
        scale = self.transform().m11()
        viewport = self.viewport().rect()
        if scale <= 0.0 or viewport.width() <= 0 or viewport.height() <= 0:
            return

        visible = self.mapToScene(viewport).boundingRect()
        margin_x = visible.width() * MARGIN
        margin_y = visible.height() * MARGIN
        target = visible.adjusted(-margin_x, -margin_y, margin_x, margin_y)

        dpr = self.devicePixelRatioF()
        width = max(1, int(round(target.width() * scale * dpr)))
        height = max(1, int(round(target.height() * scale * dpr)))
        if width * height > MAX_CACHE_PIXELS:
            return

        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(theme.qcolor(theme.CRUST))

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(width / target.width(), height / target.height())
        painter.translate(-target.left(), -target.top())
        self._paint_scene(painter, target, scale)
        painter.end()

        self._cache = QPixmap.fromImage(image)
        self._cache_rect = target
        self._cache_scale = scale
        self._cache_progress = self._progress_index
        self.viewport().update()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, theme.qcolor(theme.CRUST))
        if self._cache is None or self._cache.isNull():
            self._schedule_rebuild()
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(self._cache_rect, self._cache, QRectF(self._cache.rect()))
        if self._cache_scale > 0:
            ratio = self.transform().m11() / self._cache_scale
            if ratio < 0.8 or ratio > 1.25:
                self._schedule_rebuild()
        if self._cache_progress != self._progress_index:
            self._schedule_rebuild()


    def _paint_scene(self, painter: QPainter, rect: QRectF, scale: float) -> None:
        self._paint_bed(painter, scale)
        if self._program is not None:
            self._paint_program(painter)
        else:
            self._paint_plan(painter)

    def _paint_program(self, painter: QPainter) -> None:
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if self.show_rapids and self._program_rapids is not None:
            lines, indices = self._program_rapids
            count = int(np.searchsorted(indices, self._progress_index, side="right"))
            if count:
                painter.setPen(QPen(theme.qcolor(theme.RAPID), 0.0))
                painter.drawLines(lines[:count])

        for _depth, colour, lines, indices in self._program_buckets:
            count = int(np.searchsorted(indices, self._progress_index, side="right"))
            if count:
                painter.setPen(QPen(theme.qcolor(colour), 0.0))
                painter.drawLines(lines[:count])

        for _depth, colour, points, indices in self._program_plunges:
            count = int(np.searchsorted(indices, self._progress_index, side="right"))
            if count:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.qcolor(colour))
                for point in points[:count]:
                    painter.drawEllipse(point, 0.9, 0.9)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if self.show_program_pauses and self._program_pauses:
            painter.setPen(QPen(theme.qcolor(theme.PAUSE), 0.0))
            for x, y, index in self._program_pauses:
                if index <= self._progress_index:
                    painter.drawEllipse(QPointF(x, y), 1.1, 1.1)

    def _paint_bed(self, painter: QPainter, scale: float) -> None:
        if self._show_bed:
            painter.setPen(QPen(theme.qcolor(theme.SURFACE1), 0.0))
            painter.setBrush(theme.qcolor(theme.BASE))
            painter.drawRect(self._bed)

            step = _nice_step(60.0 / scale) if scale > 0 else 10.0
            if step > 0:
                painter.setPen(QPen(theme.qcolor(theme.SURFACE0), 0.0))
                x = 0.0
                while x <= self._bed.width() + 1e-6:
                    painter.drawLine(QPointF(x, 0.0), QPointF(x, self._bed.height()))
                    x += step
                y = 0.0
                while y <= self._bed.height() + 1e-6:
                    painter.drawLine(QPointF(0.0, y), QPointF(self._bed.width(), y))
                    y += step

    def _paint_plan(self, painter: QPainter) -> None:
        if self._show_copper:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for colour, rings in self._copper:
                painter.setPen(QPen(theme.qcolor(colour, 190), 0.0))
                for ring in rings:
                    painter.drawPolyline(ring)

        if self._show_outline and self._outline is not None:
            painter.setPen(QPen(theme.qcolor(theme.OUTLINE), 0.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(self._outline)

        for layer in self._layers:
            if not layer.visible or not layer.polygons:
                continue
            painter.setPen(QPen(theme.qcolor(layer.colour), 0.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for polygon in layer.polygons:
                painter.drawPolyline(polygon)

        if self._show_holes and self._holes:
            painter.setPen(QPen(theme.qcolor(theme.DRILL), 0.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for x, y, radius in self._holes:
                painter.drawEllipse(QPointF(x, y), radius, radius)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._paint_legend(painter)
        self._paint_cursor(painter)
        painter.end()

    def _paint_legend(self, painter: QPainter) -> None:
        if not self.show_legend:
            return
        viewport = self.viewport().rect()
        if viewport.width() < 320 or viewport.height() < 240:
            return

        entries: list[tuple[str, str]] = []
        if self._program is not None:
            if self.show_rapids and self._program_rapids is not None:
                entries.append(("Rapid travel", theme.RAPID))
            for depth, colour, lines, _indices in self._program_buckets:
                if lines:
                    entries.append((f"Cut {depth:.2f} mm", colour))
            for depth, colour, points, _indices in self._program_plunges:
                if points:
                    entries.append((f"Plunge {depth:.2f} mm", colour))
            if self.show_program_pauses and self._program_pauses:
                entries.append(("Tool change", theme.PAUSE))
        else:
            if self._show_copper:
                entries.append(("Copper", theme.COPPER_TOP))
            for layer in self._layers:
                if layer.visible and layer.polygons:
                    entries.append((layer.kind.capitalize(), layer.colour))
            if self._show_holes and self._holes:
                entries.append(("Drills", theme.DRILL))

        if not entries:
            return
        if len(entries) > 9:
            hidden = len(entries) - 8
            entries = entries[:8] + [(f"+{hidden} more depths", theme.OVERLAY)]

        painter.setFont(QFont("Segoe UI", 8))
        metrics = painter.fontMetrics()
        width = max(metrics.horizontalAdvance(text) for text, _ in entries) + 26
        height = len(entries) * 15 + 12

        panel = QRectF(12.0, 12.0, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(theme.MANTLE, 215))
        painter.drawRoundedRect(panel, 7.0, 7.0)

        y = panel.top() + 8.0
        for text, colour in entries:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.qcolor(colour))
            painter.drawRoundedRect(QRectF(panel.left() + 8.0, y + 3.0, 9.0, 9.0), 2.0, 2.0)
            painter.setPen(theme.qcolor(theme.SUBTEXT))
            painter.drawText(QPointF(panel.left() + 22.0, y + 11.0), text)
            y += 15.0

    def _paint_cursor(self, painter: QPainter) -> None:
        if not self._has_cursor:
            return
        viewport = self.viewport().rect()
        if viewport.width() < 260 or viewport.height() < 160:
            return
        scene_point = self.mapToScene(self._cursor.toPoint())
        text = f"X {scene_point.x():8.3f}   Y {self._machine[1] - scene_point.y():8.3f} mm"
        painter.setFont(QFont("Consolas", 8))
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 16
        height = metrics.height() + 8
        panel = QRectF(
            self.viewport().width() - width - 12.0,
            self.viewport().height() - height - 12.0,
            width,
            height,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(theme.MANTLE, 215))
        painter.drawRoundedRect(panel, 6.0, 6.0)
        painter.setPen(theme.qcolor(theme.SUBTEXT))
        painter.drawText(panel.adjusted(8.0, 0.0, -8.0, 0.0), Qt.AlignmentFlag.AlignVCenter, text)


def _depth_colour(depth: float, deepest: float, shallowest: float) -> str:
    if shallowest <= deepest:
        return theme.ISOLATION
    fraction = (depth - deepest) / (shallowest - deepest)
    fraction = max(0.0, min(1.0, fraction))
    shallow = theme.qcolor(theme.ISOLATION)
    deep = theme.qcolor(theme.OUTLINE)
    red = int(deep.red() + (shallow.red() - deep.red()) * fraction)
    green = int(deep.green() + (shallow.green() - deep.green()) * fraction)
    blue = int(deep.blue() + (shallow.blue() - deep.blue()) * fraction)
    return f"#{red:02X}{green:02X}{blue:02X}"


def _nice_step(raw: float) -> float:
    if raw <= 0:
        return 0.0
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for multiple in (1.0, 2.0, 5.0, 10.0):
        candidate = multiple * magnitude
        if candidate >= raw:
            return candidate
    return magnitude * 10.0
