
from __future__ import annotations

import re

from PySide6.QtGui import QColor

_AMOLED = {
    "VOID": "#000000",
    "CRUST": "#000000",
    "MANTLE": "#000000",
    "BASE": "#000000",
    "SURFACE0": "#0A0A0A",
    "SURFACE1": "#141414",
    "SURFACE2": "#1F1F1F",
    "BORDER": "#191919",
    "TEXT": "#F2F2F7",
    "SUBTEXT": "#A2A2B4",
    "OVERLAY": "#6E6E80",
    "BLUE": "#8AB4F8",
    "GREEN": "#A6E3A1",
    "RED": "#F38BA8",
    "YELLOW": "#F9E2AF",
    "MAUVE": "#CBA6F7",
    "PEACH": "#FAB387",
    "TEAL": "#94E2D5",
    "PINK": "#F5C2E7",
    "COPPER_TOP": "#FAB387",
    "COPPER_BOTTOM": "#8AB4F8",
    "OUTLINE": "#F38BA8",
    "ISOLATION": "#A6E3A1",
    "RUBOUT": "#CBA6F7",
    "CUTOUT": "#F38BA8",
    "DRILL": "#94E2D5",
    "SILKSCREEN": "#F5C2E7",
    "RAPID": "#3A3A4A",
    "PAUSE": "#F9E2AF",
    "WARN_FILL": "rgba(249, 226, 175, 24)",
    "WARN_LINE": "rgba(249, 226, 175, 64)",
}

_LIGHT = {
    "VOID": "#FFFFFF",
    "CRUST": "#FFFFFF",
    "MANTLE": "#F1F1F6",
    "BASE": "#F8F8FB",
    "SURFACE0": "#FFFFFF",
    "SURFACE1": "#EFEFF5",
    "SURFACE2": "#DEDEE8",
    "BORDER": "#D6D6E0",
    "TEXT": "#16161E",
    "SUBTEXT": "#4E4E62",
    "OVERLAY": "#83839A",
    "BLUE": "#1F6FEB",
    "GREEN": "#2E8B4F",
    "RED": "#C93B54",
    "YELLOW": "#9A6E12",
    "MAUVE": "#6B46C1",
    "PEACH": "#B5651D",
    "TEAL": "#177F76",
    "PINK": "#B23A78",
    "COPPER_TOP": "#C46A2A",
    "COPPER_BOTTOM": "#3B6FD4",
    "OUTLINE": "#C93B54",
    "ISOLATION": "#2E8B4F",
    "RUBOUT": "#6B46C1",
    "CUTOUT": "#C93B54",
    "DRILL": "#177F76",
    "SILKSCREEN": "#B23A78",
    "RAPID": "#C2C2D0",
    "PAUSE": "#9A6E12",
    "WARN_FILL": "rgba(154, 110, 18, 26)",
    "WARN_LINE": "rgba(154, 110, 18, 70)",
}

MODE = "dark"
DENSITY = "compact"

_DENSITY_SCALE = {"compact": 1.0, "comfortable": 1.22}

FONT_FAMILY = '"Segoe UI", "Inter", "Helvetica Neue", sans-serif'
MONO_FAMILY = '"Cascadia Mono", "Consolas", monospace'


def _assign(values: dict[str, str]) -> None:
    globals().update(values)


_assign(_AMOLED)


def qcolor(value: str, alpha: int | None = None) -> QColor:
    colour = QColor(value)
    if alpha is not None:
        colour.setAlpha(alpha)
    return colour


_TEMPLATE = """
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
    background-color: {WARN_FILL};
    border: 1px solid {WARN_LINE};
    border-radius: 7px;
}}
#WarningStrip QLabel {{ background: transparent; color: {YELLOW}; font-size: 11px; }}

#ViewerFrame {{
    background-color: {VOID};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
"""


def _scale_css(css: str, factor: float) -> str:
    if abs(factor - 1.0) < 1e-6:
        return css

    def bump(match) -> str:
        value = float(match.group(2)) * factor
        return f"{match.group(1)}{value:.1f}{match.group(3)}"

    css = re.sub(r"(font-size:\s*)([\d.]+)(px)", bump, css)
    css = re.sub(r"(padding:\s*)([\d.]+)(px)", bump, css)
    css = re.sub(r"(min-height:\s*)([\d.]+)(px)", bump, css)
    css = re.sub(r"((?:^|\s)width:\s*)([\d.]+)(px)", bump, css)
    return css


def stylesheet() -> str:
    values = {key: value for key, value in globals().items() if isinstance(value, str)}
    css = _TEMPLATE.format(**values)
    return _scale_css(css, _DENSITY_SCALE.get(DENSITY, 1.0))


STYLESHEET = stylesheet()


def set_mode(value: str) -> str:
    global MODE, STYLESHEET
    MODE = "light" if str(value).lower() == "light" else "dark"
    _assign(_LIGHT if MODE == "light" else _AMOLED)
    STYLESHEET = stylesheet()
    return MODE


def set_density(value: str) -> str:
    global DENSITY, STYLESHEET
    DENSITY = "comfortable" if str(value).lower() == "comfortable" else "compact"
    STYLESHEET = stylesheet()
    return DENSITY


def apply(app) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
