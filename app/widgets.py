
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme


class Card(QFrame):

    def __init__(
        self,
        title: str,
        accent: str = theme.BLUE,
        expanded: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 12)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)

        bar = QFrame()
        bar.setFixedSize(3, 14)
        bar.setStyleSheet(f"background-color: {accent}; border-radius: 2px;")
        header.addWidget(bar)

        self._title = QLabel(title)
        self._title.setObjectName("CardTitle")
        header.addWidget(self._title)
        header.addStretch(1)

        self._toggle = QToolButton()
        self._toggle.setCheckable(True)
        self._toggle.setChecked(expanded)
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._toggle.setStyleSheet(
            f"QToolButton {{ background: transparent; border: none; color: {theme.SUBTEXT}; }}"
            f"QToolButton:hover {{ color: {theme.TEXT}; }}"
        )
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.clicked.connect(self._on_toggle)
        self._toggle.setFixedSize(22, 22)
        header.addWidget(self._toggle)

        outer.addLayout(header)

        self.body = QWidget()
        self.body.setObjectName("CardBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(7)
        self.body.setVisible(expanded)
        outer.addWidget(self.body)

    def _on_toggle(self, checked: bool) -> None:
        self.body.setVisible(checked)
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )

    def add(self, widget: QWidget) -> QWidget:
        self.body_layout.addWidget(widget)
        return widget

    def add_row(self, label: str, widget: QWidget) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        caption = QLabel(label)
        caption.setObjectName("StatLabel")
        caption.setMinimumWidth(112)
        layout.addWidget(caption)
        layout.addWidget(widget, 1)
        self.body_layout.addWidget(row)
        return row

    def add_label(self, text: str, style: str = "Hint") -> QLabel:
        label = QLabel(text)
        label.setObjectName(style)
        label.setWordWrap(True)
        self.body_layout.addWidget(label)
        return label

    def add_section_label(self, text: str) -> QLabel:
        return self.add_label(text, "SectionLabel")

    def add_stat(self, label: str, value: str, colour: str = theme.TEXT) -> "StatRow":
        row = StatRow(label, value, colour)
        self.body_layout.addWidget(row)
        return row


class StatRow(QWidget):

    def __init__(self, label: str, value: str, colour: str = theme.TEXT) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._label = QLabel(label)
        self._label.setObjectName("StatLabel")
        layout.addWidget(self._label)
        layout.addStretch(1)

        self._value = QLabel(value)
        self._value.setObjectName("StatValue")
        self._value.setStyleSheet(f"color: {colour};")
        layout.addWidget(self._value)

    def set_label(self, label: str) -> None:
        self._label.setText(label)

    def set_value(self, value: str, colour: str | None = None) -> None:
        self._value.setText(value)
        if colour:
            self._value.setStyleSheet(f"color: {colour};")


class SliderRow(QWidget):

    valueChanged = Signal(float)

    def __init__(
        self,
        label: str,
        minimum: float,
        maximum: float,
        value: float,
        decimals: int = 2,
        suffix: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._decimals = decimals
        self._suffix = suffix
        self._scale = 10 ** decimals

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        caption = QLabel(label)
        caption.setObjectName("StatLabel")
        caption.setMinimumWidth(96)
        caption.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        layout.addWidget(caption)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setMinimum(int(round(minimum * self._scale)))
        self._slider.setMaximum(int(round(maximum * self._scale)))
        self._slider.setValue(int(round(value * self._scale)))
        self._slider.setPageStep(max(1, int(self._scale)))
        self._slider.setMinimumWidth(40)
        layout.addWidget(self._slider, 1)

        if decimals == 0:
            self._spin: QDoubleSpinBox | QSpinBox = QSpinBox()
            self._spin.setRange(int(minimum), int(maximum))
            self._spin.setValue(int(value))
            self._spin.setSuffix(suffix)
        else:
            self._spin = QDoubleSpinBox()
            self._spin.setDecimals(decimals)
            self._spin.setRange(minimum, maximum)
            self._spin.setSingleStep(1.0 / self._scale)
            self._spin.setValue(value)
            self._spin.setSuffix(suffix)

        from PySide6.QtWidgets import QAbstractSpinBox

        self._spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._spin.setMinimumWidth(72)
        self._spin.setMaximumWidth(94)
        layout.addWidget(self._spin)

        self._slider.valueChanged.connect(self._on_slider)
        self._spin.valueChanged.connect(self._on_spin)

    def _on_slider(self, raw: int) -> None:
        value = raw / self._scale
        self._spin.blockSignals(True)
        self._spin.setValue(value)
        self._spin.blockSignals(False)
        self.valueChanged.emit(float(value))

    def _on_spin(self, value: float) -> None:
        raw = int(round(float(value) * self._scale))
        self._slider.blockSignals(True)
        self._slider.setValue(raw)
        self._slider.blockSignals(False)
        self.valueChanged.emit(float(value))

    def setToolTip(self, text: str) -> None:
        super().setToolTip(text)
        self._slider.setToolTip(text)
        self._spin.setToolTip(text)

    def value(self) -> float:
        return float(self._spin.value())

    def set_value(self, value: float) -> None:
        self._slider.blockSignals(True)
        self._spin.blockSignals(True)
        self._spin.setValue(value)
        self._slider.setValue(int(round(value * self._scale)))
        self._slider.blockSignals(False)
        self._spin.blockSignals(False)

    def set_enabled(self, enabled: bool) -> None:
        self._slider.setEnabled(enabled)
        self._spin.setEnabled(enabled)


class ToggleRow(QWidget):

    toggled = Signal(bool)

    def __init__(self, text: str, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._box = QCheckBox(text)
        self._box.setChecked(checked)
        self._box.toggled.connect(self.toggled.emit)
        layout.addWidget(self._box)
        layout.addStretch(1)

    def setToolTip(self, text: str) -> None:
        super().setToolTip(text)
        self._box.setToolTip(text)

    def isChecked(self) -> bool:
        return self._box.isChecked()

    def setChecked(self, value: bool) -> None:
        self._box.blockSignals(True)
        self._box.setChecked(value)
        self._box.blockSignals(False)
