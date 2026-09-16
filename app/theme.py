
from __future__ import annotations

from PySide6.QtGui import QColor

VOID = "#040406"
CRUST = "#07070B"
MANTLE = "#0A0A0F"
BASE = "#0C0C12"
SURFACE0 = "#12121A"
SURFACE1 = "#1A1A24"
SURFACE2 = "#242430"
BORDER = "#1C1C26"

TEXT = "#E8E8F4"
SUBTEXT = "#9A9AB4"
OVERLAY = "#5A5A72"

BLUE = "#89B4FA"
GREEN = "#A6E3A1"
RED = "#F38BA8"
YELLOW = "#F9E2AF"
MAUVE = "#CBA6F7"
PEACH = "#FAB387"
TEAL = "#94E2D5"
PINK = "#F5C2E7"

COPPER_TOP = PEACH
COPPER_BOTTOM = BLUE
OUTLINE = RED
ISOLATION = GREEN
RUBOUT = MAUVE
CUTOUT = RED
DRILL = TEAL
SILKSCREEN = PINK
RAPID = "#4E4E6E"
PAUSE = YELLOW

FONT_FAMILY = '"Segoe UI", "Inter", "Helvetica Neue", sans-serif'
MONO_FAMILY = '"Cascadia Mono", "Consolas", monospace'


def qcolor(value: str, alpha: int | None = None) -> QColor:
    colour = QColor(value)
    if alpha is not None:
        colour.setAlpha(alpha)
    return colour


STYLESHEET = f"""
QWidget {{
    background-color: {BASE};
    color: {TEXT};
    font-family: {FONT_FAMILY};
    font-size: 12px;
}}

QToolTip {{
    background-color: {SURFACE1};
    color: {TEXT};
    border: 1px solid {SURFACE2};
    border-radius: 6px;
    padding: 5px 8px;
}}

/* ---------------------------------------------------------------- header -- */
#Header {{
    background-color: {MANTLE};
    border-bottom: 1px solid {BORDER};
}}
#Title {{
    font-size: 16px;
    font-weight: 600;
    color: {BLUE};
    background: transparent;
}}
#Subtitle {{
    font-size: 11px;
    color: {OVERLAY};
    background: transparent;
}}

/* ----------------------------------------------------------------- cards -- */
#Card {{
    background-color: {SURFACE0};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
#CardTitle {{
    font-size: 12.5px;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}
#CardBody {{ background: transparent; }}
#SectionLabel {{ color: {SUBTEXT}; font-size: 11.5px; background: transparent; }}
#Hint {{ color: {OVERLAY}; font-size: 11px; background: transparent; }}
#StatLabel {{ color: {SUBTEXT}; font-size: 11.5px; background: transparent; }}
#StatValue {{ font-size: 11.5px; font-weight: 600; background: transparent; }}

/* --------------------------------------------------------------- buttons -- */
QPushButton {{
    background-color: {SURFACE1};
    color: {TEXT};
    border: 1px solid {SURFACE2};
    border-radius: 7px;
    padding: 6px 12px;
    font-size: 12px;
}}
QPushButton:hover {{ background-color: {SURFACE2}; }}
QPushButton:pressed {{ background-color: {SURFACE0}; }}
QPushButton:disabled {{
    background-color: {SURFACE0};
    color: {OVERLAY};
    border-color: {BORDER};
}}

QPushButton#Primary {{
    background-color: {GREEN};
    color: {VOID};
    border: none;
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background-color: #B6EBAF; }}
QPushButton#Primary:pressed {{ background-color: #8FCF8B; }}

QPushButton#Accent {{
    background-color: {BLUE};
    color: {VOID};
    border: none;
    font-weight: 600;
}}
QPushButton#Accent:hover {{ background-color: #9CC1FB; }}
QPushButton#Accent:pressed {{ background-color: #77A2E6; }}

QPushButton#Ghost {{
    background: transparent;
    border: 1px solid {BORDER};
    color: {SUBTEXT};
}}
QPushButton#Ghost:hover {{ border-color: {SURFACE2}; color: {TEXT}; }}

/* ---------------------------------------------------------------- inputs -- */
QSlider::groove:horizontal {{
    height: 4px;
    background: {SURFACE1};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {BLUE}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT};
    width: 13px;
    height: 13px;
    margin: -5px 0;
    border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: {BLUE}; }}
QSlider::handle:horizontal:disabled {{ background: {OVERLAY}; }}
QSlider::sub-page:horizontal:disabled {{ background: {SURFACE2}; }}

QDoubleSpinBox, QSpinBox {{
    background-color: {CRUST};
    border: 1px solid {SURFACE1};
    border-radius: 6px;
    padding: 3px 6px;
    color: {TEXT};
    font-family: {MONO_FAMILY};
    font-size: 11.5px;
    selection-background-color: {BLUE};
    selection-color: {VOID};
}}
QDoubleSpinBox:focus, QSpinBox:focus {{ border-color: {BLUE}; }}
QDoubleSpinBox:disabled, QSpinBox:disabled {{ color: {OVERLAY}; }}

QComboBox {{
    background-color: {CRUST};
    border: 1px solid {SURFACE1};
    border-radius: 6px;
    padding: 4px 8px;
    min-width: 80px;
}}
QComboBox:hover {{ border-color: {SURFACE2}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 20px;
    border: none;
    background: transparent;
}}
/* Zero-sized box + borders = a crisp triangle (needs the explicit 0 sizing,
   otherwise Qt gives the sub-control a default box and draws a square). */
QComboBox::down-arrow {{
    image: none;
    width: 0;
    height: 0;
    margin-right: 8px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {SUBTEXT};
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE0};
    border: 1px solid {SURFACE2};
    border-radius: 6px;
    selection-background-color: {BLUE};
    selection-color: {VOID};
    outline: none;
}}

QCheckBox {{ spacing: 7px; background: transparent; }}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {SURFACE2};
    border-radius: 4px;
    background-color: {CRUST};
}}
QCheckBox::indicator:hover {{ border-color: {BLUE}; }}
QCheckBox::indicator:checked {{ background-color: {BLUE}; border-color: {BLUE}; }}
QCheckBox::indicator:disabled {{ border-color: {BORDER}; }}

/* ------------------------------------------------------------ containers -- */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {SURFACE2};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {OVERLAY}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; }}
QScrollBar::handle:horizontal {{
    background: {SURFACE2}; border-radius: 4px; min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ---------------------------------------------------------------- status -- */
#StatusBar {{
    background-color: {MANTLE};
    border-top: 1px solid {BORDER};
}}
#StatusText {{ color: {SUBTEXT}; font-size: 11.5px; background: transparent; }}
#StatusKey {{ color: {OVERLAY}; font-size: 11px; background: transparent; }}
#StatusValue {{ font-size: 11.5px; font-weight: 600; background: transparent; }}

#WarningStrip {{
    background-color: rgba(249, 226, 175, 24);
    border: 1px solid rgba(249, 226, 175, 64);
    border-radius: 7px;
}}
#WarningStrip QLabel {{ background: transparent; color: {YELLOW}; font-size: 11px; }}

#ViewerFrame {{
    background-color: {VOID};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
"""


def apply(app) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
