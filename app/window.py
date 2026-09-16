
from __future__ import annotations

import math
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from exporter import ExportPackage, verify_plan, write_package
from gcode_reader import read_file as read_gcode_file
from gerber_io import LayerType, PcbProject, load_project
from pcb_engine import SlicerConfig, SlicerError, ToolSpec, plan_toolpaths
from tool_db import (
    DbTool,
    ToolDatabase,
    ToolDbError,
    apply_role,
    estimate_min_clearance,
    load_tool_db,
    recommend_tools,
)
from updater import UpdateInfo, check_for_update
from wegstr_gcode import GCodeError, MachineProfile, estimate_runtime

from . import theme
from .settings import Preferences
from .viewer import ToolpathView
from .widgets import Card, SliderRow, StatRow, ToggleRow

DEFAULT_ARCHIVE = Path(
    r"C:\Users\charles\Downloads\Gerber_Turretv2_PCB_Turretv2_2026-09-15.zip"
)

REPLAN_DELAY_MS = 320

PLAYBACK_SECONDS = 22.0

AUTOSAVE_DELAY_MS = 1200


class Planner(QObject):

    finished = Signal(object, object)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._alive = True

    def request(
        self, project: PcbProject, config: SlicerConfig, profile: MachineProfile
    ) -> None:
        thread = threading.Thread(
            target=self._run, args=(project, config, profile), daemon=True
        )
        thread.start()

    def shutdown(self) -> None:
        self._alive = False

    def _run(
        self, project: PcbProject, config: SlicerConfig, profile: MachineProfile
    ) -> None:
        with self._lock:
            try:
                plan = plan_toolpaths(project, config)
                report = verify_plan(plan, config, profile)
            except (SlicerError, GCodeError) as exc:
                if self._alive:
                    self.failed.emit(str(exc))
                return
            except Exception as exc:
                if self._alive:
                    self.failed.emit(f"{type(exc).__name__}: {exc}")
                return
            if self._alive:
                self.finished.emit(plan, report)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Wegstr PCB Slicer")
        self._size_to_screen()

        self.config = SlicerConfig()
        self.profile = MachineProfile()
        self.project: PcbProject | None = None
        self.archive: Path | None = None
        self.plan = None
        self.verification = None
        self.package: ExportPackage | None = None
        self._bindings: list[tuple[object, object, object]] = []
        self._panel_hidden = False
        self._ui_ready = False
        self._sides_defaulted_for: str | None = None
        self._stat_pairs: list[tuple[QFrame, QLabel, QLabel]] = []
        self.viewer_toggles: dict[str, ToggleRow] = {}

        self.tool_db: ToolDatabase | None = None
        self.tool_combos: dict[str, QComboBox] = {}
        self._tools_auto_applied = False
        self._update_info: UpdateInfo | None = None

        self.preferences = Preferences()
        self.preferences.restore_job(self.config, self.profile)
        self._panel_hidden = self.preferences.restore_panel_hidden()
        self._load_tool_db()

        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(AUTOSAVE_DELAY_MS)
        self._autosave.timeout.connect(self._save_preferences)

        self.planner = Planner()
        self.planner.finished.connect(self._on_plan_ready)
        self.planner.failed.connect(self._on_plan_failed)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(REPLAN_DELAY_MS)
        self._debounce.timeout.connect(self._request_plan)

        self.program = None
        self._gcode_mode = False
        self._play_position = 1.0
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(33)
        self._play_timer.timeout.connect(self._advance_playback)

        self._build_ui()
        self._install_shortcuts()
        self._populate_tool_combos()

        if self.preferences.restore_window(self):
            QTimer.singleShot(0, self._clamp_to_current_screen)

        self.viewer.apply_visibility(self.preferences.restore_viewer())
        self._sync_viewer_toggles()
        self._refresh_widgets()

        startup = self._startup_archive()
        if startup is not None:
            QTimer.singleShot(60, lambda: self._load(startup))

        QTimer.singleShot(1400, self._check_for_updates)

    def _startup_archive(self) -> Path | None:
        remembered = self.preferences.restore_archive()
        if remembered:
            candidate = Path(remembered)
            if candidate.exists():
                return candidate
        if DEFAULT_ARCHIVE.exists():
            return DEFAULT_ARCHIVE
        return None


    def _size_to_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1280, 820)
            self.setMinimumSize(900, 600)
            return

        available = screen.availableGeometry()
        width = max(760, min(1280, available.width() - 40))
        height = max(500, min(840, available.height() - 60))
        self.resize(width, height)
        self.setMinimumSize(min(720, width), min(470, height))

    def _clamp_to_current_screen(self) -> None:
        if self.isMaximized() or self.isFullScreen():
            return

        screens = QGuiApplication.screens()
        if not screens:
            return

        frame = self.frameGeometry()
        centre = frame.center()
        screen = next(
            (s for s in screens if s.availableGeometry().contains(centre)), None
        )
        if screen is None:
            screen = min(
                screens,
                key=lambda s: abs(s.availableGeometry().center().x() - centre.x())
                + abs(s.availableGeometry().center().y() - centre.y()),
            )

        available = screen.availableGeometry()
        width = min(self.width(), max(720, available.width() - 24))
        height = min(self.height(), max(470, available.height() - 40))
        if (width, height) != (self.width(), self.height()):
            self.resize(width, height)

        frame = self.frameGeometry()
        x = min(
            max(frame.left(), available.left()),
            available.right() - frame.width() + 1,
        )
        y = min(
            max(frame.top(), available.top()),
            available.bottom() - frame.height() + 1,
        )
        if (x, y) != (frame.left(), frame.top()):
            self.move(x, y)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._clamp_to_current_screen()
        QTimer.singleShot(150, self._clamp_to_current_screen)

    def _on_panel_toggled(self, visible: bool) -> None:
        self._panel_hidden = not visible
        self._apply_responsive_layout()
        self._schedule_autosave()

    def _apply_responsive_layout(self) -> None:
        if not getattr(self, "_ui_ready", False):
            return

        width = self.width()

        panel = getattr(self, "_side_panel", None)
        if panel is not None:
            panel.setVisible(not self._panel_hidden)
            if not self._panel_hidden:
                content = panel.widget()
                floor = (
                    content.minimumSizeHint().width() + 16
                    if content is not None
                    else 250
                )
                target = max(floor, min(380, int(width * 0.34)))
                panel.setFixedWidth(min(target, max(floor, width - 300)))

        self.title_label.setVisible(width >= 620)
        self.subtitle.setVisible(width >= 1120)
        self.reload_button.setVisible(width >= 980)
        self.quality_button.setVisible(width >= 900)
        self.view_button.setVisible(width >= 1120)
        self.view_button.setText("View G-code…" if width >= 1260 else "G-code…")
        self.oneclick_button.setText(
            "One-Click Isolation" if width >= 1180 else "One-Click"
        )
        self.export_button.setText("Export Files…" if width >= 1020 else "Export")

        detailed = width >= 940
        for separator, key, value in self._stat_pairs:
            separator.setVisible(detailed)
            key.setVisible(detailed)
            value.setVisible(detailed)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 10)
        body_layout.setSpacing(10)

        viewer_frame = QFrame()
        viewer_frame.setObjectName("ViewerFrame")
        viewer_layout = QVBoxLayout(viewer_frame)
        viewer_layout.setContentsMargins(1, 1, 1, 1)
        viewer_layout.setSpacing(0)

        self.viewer = ToolpathView()
        self.viewer.cursorMoved.connect(self._on_cursor_moved)
        viewer_layout.addWidget(self.viewer)

        self.warning_strip = self._build_warning_strip()
        viewer_layout.addWidget(self.warning_strip)

        self.update_strip = self._build_update_strip()
        viewer_layout.addWidget(self.update_strip)

        body_layout.addWidget(viewer_frame, 1)
        body_layout.addWidget(self._build_side_panel())

        root.addWidget(body, 1)
        root.addWidget(self._build_transport())
        root.addWidget(self._build_status())
        self.setCentralWidget(central)
        self._ui_ready = True
        self._apply_responsive_layout()

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(60)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(10)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title_label = QLabel("Wegstr PCB Slicer")
        self.title_label.setObjectName("Title")
        titles.addWidget(self.title_label)
        self.subtitle = QLabel(self.profile.name)
        self.subtitle.setObjectName("Subtitle")
        titles.addWidget(self.subtitle)
        layout.addLayout(titles)

        layout.addSpacing(10)
        layout.addWidget(_vline())

        self.open_button = QPushButton("Open Gerber…")
        self.open_button.setToolTip("Open a Gerber .zip archive")
        self.open_button.clicked.connect(self._choose_archive)
        layout.addWidget(self.open_button)

        self.reload_button = QPushButton("Reload")
        self.reload_button.clicked.connect(self._reload)
        layout.addWidget(self.reload_button)

        layout.addSpacing(6)

        self.oneclick_button = QPushButton("One-Click Isolation")
        self.oneclick_button.setObjectName("Primary")
        self.oneclick_button.setToolTip("Conservative defaults, then compute and export")
        self.oneclick_button.clicked.connect(self._one_click)
        layout.addWidget(self.oneclick_button)

        self.quality_button = QPushButton("High Quality")
        self.quality_button.setToolTip("Three isolation passes plus rub-out copper clearing")
        self.quality_button.clicked.connect(self._apply_quality_preset)
        layout.addWidget(self.quality_button)

        layout.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedSize(90, 6)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet(
            f"QProgressBar {{ background: {theme.SURFACE0}; border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {theme.BLUE}; border-radius: 3px; }}"
        )
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.view_button = QPushButton("View G-code…")
        self.view_button.setToolTip(
            "Open a .gcode file and inspect the real machine moves, including\n"
            "rapids, plunges and tool changes"
        )
        self.view_button.clicked.connect(self._open_gcode)
        layout.addWidget(self.view_button)

        self.panel_button = QPushButton("Panel")
        self.panel_button.setObjectName("Ghost")
        self.panel_button.setCheckable(True)
        self.panel_button.setChecked(True)
        self.panel_button.setToolTip("Show or hide the settings panel")
        self.panel_button.setFixedWidth(58)
        self.panel_button.toggled.connect(self._on_panel_toggled)
        layout.addWidget(self.panel_button)

        self.recompute_button = QPushButton("Recompute")
        self.recompute_button.setToolTip("Recompute the toolpaths (Ctrl+R)")
        self.recompute_button.clicked.connect(self._request_plan)
        layout.addWidget(self.recompute_button)

        self.export_button = QPushButton("Export Files…")
        self.export_button.setObjectName("Accent")
        self.export_button.setToolTip(
            "Write one G-code file per operation plus a step-by-step README (Ctrl+E)"
        )
        self.export_button.clicked.connect(self._export)
        layout.addWidget(self.export_button)

        return header

    def _build_warning_strip(self) -> QWidget:
        strip = QFrame()
        strip.setObjectName("WarningStrip")
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(10, 6, 10, 6)
        self.warning_label = QLabel("")
        layout.addWidget(self.warning_label)
        layout.addStretch(1)
        strip.setVisible(False)
        return strip

    def _build_update_strip(self) -> QWidget:
        strip = QFrame()
        strip.setObjectName("WarningStrip")
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(10, 6, 10, 6)
        self.update_label = QLabel("")
        layout.addWidget(self.update_label)
        layout.addStretch(1)
        self.update_button = QPushButton("Download")
        self.update_button.setFixedWidth(100)
        self.update_button.clicked.connect(self._open_update_page)
        layout.addWidget(self.update_button)
        dismiss = QPushButton("Dismiss")
        dismiss.setFixedWidth(84)
        dismiss.clicked.connect(lambda: strip.setVisible(False))
        layout.addWidget(dismiss)
        strip.setVisible(False)
        return strip

    def _open_update_page(self) -> None:
        info = self._update_info
        if info is not None and info.url:
            QDesktopServices.openUrl(QUrl(info.url))

    def _build_side_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel_width = max(300, min(370, int(self.width() * 0.30)))
        scroll.setFixedWidth(panel_width)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(8)

        self._build_help_card(layout)
        self._build_job_card(layout)
        self._build_tool_card(layout)
        self._build_isolation_card(layout)
        self._build_rubout_card(layout)
        self._build_cutout_card(layout)
        self._build_drill_card(layout)
        self._build_silkscreen_card(layout)
        self._build_alignment_card(layout)
        self._build_machine_card(layout)
        self._build_view_card(layout)
        self._build_verify_card(layout)
        self._build_report_card(layout)

        layout.addStretch(1)
        scroll.setWidget(container)
        self._side_panel = scroll
        return scroll

    def _build_transport(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("StatusBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 5, 14, 5)
        layout.setSpacing(8)

        self.transport_label = QLabel("—")
        self.transport_label.setObjectName("StatusText")
        self.transport_label.setMinimumWidth(150)
        layout.addWidget(self.transport_label)

        self.play_button = QPushButton("Play")
        self.play_button.setFixedWidth(64)
        self.play_button.clicked.connect(self._toggle_playback)
        layout.addWidget(self.play_button)

        self.scrubber = QSlider(Qt.Orientation.Horizontal)
        self.scrubber.setRange(0, 1000)
        self.scrubber.setValue(1000)
        self.scrubber.valueChanged.connect(self._on_scrub)
        layout.addWidget(self.scrubber, 1)

        self.transport_readout = QLabel("")
        self.transport_readout.setObjectName("StatusKey")
        self.transport_readout.setMinimumWidth(210)
        layout.addWidget(self.transport_readout)

        self.show_rapids_box = ToggleRow("Rapids", True)
        self.show_rapids_box.toggled.connect(self._on_rapids_toggled)
        layout.addWidget(self.show_rapids_box)
        self.viewer_toggles["rapids"] = self.show_rapids_box

        self.show_pauses_box = ToggleRow("Pauses", True)
        self.show_pauses_box.toggled.connect(self._on_pauses_toggled)
        layout.addWidget(self.show_pauses_box)
        self.viewer_toggles["pauses"] = self.show_pauses_box

        close = QPushButton("Back to toolpaths")
        close.setObjectName("Ghost")
        close.clicked.connect(self._exit_gcode_mode)
        layout.addWidget(close)

        self.transport = bar
        bar.setVisible(False)
        return bar

    def _build_status(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(30)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 4, 14, 4)
        layout.setSpacing(8)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("StatusText")
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.stat_runtime = _status_stat("runtime", theme.GREEN)
        self.stat_cut = _status_stat("cut", theme.TEXT)
        self.stat_paths = _status_stat("paths", theme.TEXT)
        self.stat_holes = _status_stat("holes", theme.TEXT)
        self._stat_pairs = []
        for pair in (self.stat_runtime, self.stat_cut, self.stat_paths, self.stat_holes):
            separator = _vline()
            layout.addWidget(separator)
            layout.addWidget(pair[0])
            layout.addWidget(pair[1])
            self._stat_pairs.append((separator, pair[0], pair[1]))

        return bar


    def _bind_slider(
        self,
        card: Card,
        label: str,
        minimum: float,
        maximum: float,
        getter,
        setter,
        decimals: int = 2,
        suffix: str = "",
    ) -> SliderRow:
        row = SliderRow(label, minimum, maximum, getter(), decimals, suffix)
        row.valueChanged.connect(lambda value: self._apply(setter, value))
        card.add(row)
        self._bindings.append((row, getter, setter))
        return row

    def _bind_toggle(self, card: Card, label: str, getter, setter) -> ToggleRow:
        row = ToggleRow(label, getter())
        row.toggled.connect(lambda value: self._apply(setter, value))
        card.add(row)
        self._bindings.append((row, getter, setter))
        return row

    def _plain_toggle(
        self, card: Card, label: str, initial: bool, callback, key: str | None = None
    ) -> ToggleRow:
        row = ToggleRow(label, initial)

        def handle(value: bool) -> None:
            callback(value)
            self._schedule_autosave()

        row.toggled.connect(handle)
        card.add(row)
        if key:
            self.viewer_toggles[key] = row
        return row

    def _build_help_card(self, layout: QVBoxLayout) -> None:
        card = Card("How to use this", theme.BLUE)
        layout.addWidget(card)
        steps = (
            ("1", "Open Gerber…", "pick the .zip your PCB tool exported."),
            ("2", "Check the preview", "green lines are the isolation cuts, red is the board outline."),
            ("3", "Pick a preset", "One-Click Isolation for speed, High Quality to also clear the copper pour."),
            ("4", "Export Files…", "writes the G-code plus a README with step-by-step instructions."),
            ("5", "Follow the README", "run the numbered files in order, changing tools when paused."),
        )
        for number, title, detail in steps:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            badge = QLabel(number)
            badge.setFixedSize(18, 18)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(
                f"background: {theme.SURFACE1}; color: {theme.TEXT};"
                "border-radius: 9px; font-size: 10px; font-weight: 600;"
            )
            row_layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
            text = QLabel(
                f"<b style='color:{theme.TEXT}'>{title}</b> "
                f"<span style='color:{theme.OVERLAY}'>{detail}</span>"
            )
            text.setWordWrap(True)
            row_layout.addWidget(text, 1)
            card.add(row)

    def _build_job_card(self, layout: QVBoxLayout) -> None:
        card = Card("Job Setup", theme.BLUE)
        layout.addWidget(card)

        self.origin_combo = QComboBox()
        self.origin_combo.addItem("Lower-left corner", "lower_left")
        self.origin_combo.addItem("Board centre", "center")
        self.origin_combo.currentIndexChanged.connect(
            lambda _: self._apply(
                lambda v: setattr(self.config, "origin_mode", v),
                self.origin_combo.currentData(),
            )
        )
        card.add_row("Work origin", self.origin_combo)

        self._bind_slider(
            card, "Margin", 0.0, 20.0,
            lambda: self.config.margin,
            lambda v: setattr(self.config, "margin", v),
            1, " mm",
        )
        self._bind_toggle(
            card, "Front copper (top)",
            lambda: self.config.mill_top,
            lambda v: setattr(self.config, "mill_top", v),
        )
        self._bind_toggle(
            card, "Back copper (bottom)",
            lambda: self.config.mill_bottom,
            lambda v: setattr(self.config, "mill_bottom", v),
        )
        self._bind_toggle(
            card, "Mirror bottom artwork",
            lambda: self.config.mirror_bottom,
            lambda v: setattr(self.config, "mirror_bottom", v),
        )
        self._bind_slider(
            card, "Board thickness", 0.4, 3.2,
            lambda: self.config.board_thickness,
            lambda v: setattr(self.config, "board_thickness", v),
            2, " mm",
        )
        self.sides_stat = card.add_stat("Sides in the Gerber", "—", theme.OVERLAY)
        self.fit_stat = card.add_stat("Work area check", "—", theme.OVERLAY)
        self.sides_hint = card.add_label("", "Hint")
        self.sides_hint.setStyleSheet(f"color: {theme.YELLOW};")
        self.sides_hint.setVisible(False)

        card.add_section_label("Output")
        self._bind_toggle(
            card, "Split into one file per operation",
            lambda: self.config.split_output,
            lambda v: setattr(self.config, "split_output", v),
        ).setToolTip(
            "On: traces / drill / outline become separate files you run one at a\n"
            "time. Off: a single combined program."
        )
        self._bind_toggle(
            card, "Drill before traces",
            lambda: self.config.drill_first,
            lambda v: setattr(self.config, "drill_first", v),
        ).setToolTip(
            "Drill first so you can drop pins into the holes and re-seat the board\n"
            "after flipping it. Required for reliable double-sided boards."
        )
        self._bind_slider(
            card, "Registration pins", 0, 4,
            lambda: self.config.registration_pins,
            lambda v: setattr(self.config, "registration_pins", int(v)),
            0,
        ).setToolTip(
            "How many existing through-holes to recommend as alignment pins in\n"
            "the generated guide. Needs 'Drill before traces'."
        )

    def _build_tool_card(self, layout: QVBoxLayout) -> None:
        card = Card("Tooling", theme.PEACH)
        layout.addWidget(card)

        self.tool_db_label = card.add_label("", "Hint")
        self.tool_db_label.setWordWrap(True)

        for role, label in (
            ("isolation", "Isolation tool"),
            ("rubout", "Rub-out tool"),
            ("cutout", "Cut-out tool"),
            ("silkscreen", "Silkscreen tool"),
        ):
            combo = QComboBox()
            combo.currentIndexChanged.connect(
                lambda _index, name=role: self._on_tool_combo_changed(name)
            )
            card.add_row(label, combo)
            self.tool_combos[role] = combo

        self.auto_tool_button = QPushButton("Auto-select from tool database")
        self.auto_tool_button.setToolTip(
            "Measure the copper clearance on the board and pick the largest\n"
            "tool that fits, plus a cut-out and silkscreen bit."
        )
        self.auto_tool_button.clicked.connect(lambda: self._auto_select_tools(force=True))
        card.add(self.auto_tool_button)

        card.add_section_label("Isolation geometry")
        self.tool_kind_combo = QComboBox()
        self.tool_kind_combo.addItem("V-bit", "vbit")
        self.tool_kind_combo.addItem("Flat end mill", "flat")
        self.tool_kind_combo.currentIndexChanged.connect(self._on_tool_kind_changed)
        card.add_row("Type", self.tool_kind_combo)

        self.angle_row = self._bind_slider(
            card, "Included angle", 10.0, 90.0,
            lambda: self.config.isolation_tool.angle,
            lambda v: setattr(self.config.isolation_tool, "angle", v),
            0, "°",
        )
        self.tip_row = self._bind_slider(
            card, "Tip flat", 0.0, 0.5,
            lambda: self.config.isolation_tool.tip_diameter,
            lambda v: setattr(self.config.isolation_tool, "tip_diameter", v),
            3, " mm",
        )
        self.tool_diameter_row = self._bind_slider(
            card, "Diameter", 0.05, 1.0,
            lambda: self.config.isolation_tool.diameter,
            lambda v: setattr(self.config.isolation_tool, "diameter", v),
            3, " mm",
        )
        self.cut_width_stat = card.add_stat("Effective cut width", "—", theme.TEAL)

        card.add_section_label("Manual overrides")
        self._bind_slider(
            card, "Cut-out diameter", 0.2, 3.0,
            lambda: self.config.cutout_tool.diameter,
            lambda v: setattr(self.config.cutout_tool, "diameter", v),
            2, " mm",
        )
        self._bind_slider(
            card, "Rub-out diameter", 0.2, 3.0,
            lambda: self.config.rubout_tool.diameter,
            lambda v: setattr(self.config.rubout_tool, "diameter", v),
            2, " mm",
        )
        self._bind_slider(
            card, "Silkscreen diameter", 0.1, 1.0,
            lambda: self.config.silkscreen_tool.diameter,
            lambda v: setattr(self.config.silkscreen_tool, "diameter", v),
            2, " mm",
        )

    def _load_tool_db(self) -> None:
        try:
            self.tool_db = load_tool_db()
        except ToolDbError as exc:
            self.tool_db = None
            self._tool_db_error = str(exc)
        else:
            self._tool_db_error = None

    def _update_tool_db_label(self) -> None:
        if not hasattr(self, "tool_db_label"):
            return
        if self.tool_db is not None:
            self.tool_db_label.setText(
                f"{len(self.tool_db)} tools from {self.tool_db.path.name} "
                f"({self.tool_db.machine or 'unknown machine'})"
            )
            self.tool_db_label.setStyleSheet(f"color: {theme.OVERLAY};")
        else:
            self.tool_db_label.setText(
                "No Vectric tool database found; using built-in defaults. "
                "Set WEGSTR_TOOL_DB to point at a .vtdb file."
            )
            self.tool_db_label.setStyleSheet(f"color: {theme.YELLOW};")

    def _populate_tool_combos(self) -> None:
        for role, combo in self.tool_combos.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Custom / manual", None)
            if self.tool_db is not None:
                for group, tools in self.tool_db.groups().items():
                    for tool in tools:
                        combo.addItem(f"{tool.name}   ·   {group}", tool.key)
            combo.blockSignals(False)
        self._update_tool_db_label()

    def _sync_tool_combos(self) -> None:
        for role, combo in self.tool_combos.items():
            tool = getattr(self.config, f"{role}_tool", None)
            source = getattr(tool, "source", "") if tool is not None else ""
            index = combo.findData(source) if source else 0
            if index < 0:
                index = 0
            if index != combo.currentIndex():
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)

    def _on_tool_combo_changed(self, role: str) -> None:
        combo = self.tool_combos.get(role)
        if combo is None or self.tool_db is None:
            return
        key = combo.currentData()
        if not key:
            return
        tool = self.tool_db.by_key(str(key))
        if tool is None:
            return
        apply_role(self.config, role, tool)
        self._refresh_widgets()
        self._debounce.start()
        self._schedule_autosave()

    def _auto_select_tools(self, force: bool = False) -> None:
        if self.tool_db is None:
            self.status_label.setText(
                "No tool database found. Set WEGSTR_TOOL_DB to a .vtdb file."
            )
            return
        if self._tools_auto_applied and not force:
            return

        gap = None
        if self.project is not None:
            for layer in self.project.layers:
                if layer.layer_type.is_copper and layer.geometry is not None:
                    gap = estimate_min_clearance(layer.geometry)
                    break

        try:
            recommendation = recommend_tools(
                self.tool_db,
                isolation_depth=self.config.isolation_depth,
                min_gap=gap,
            )
        except ToolDbError as exc:
            self.status_label.setText(str(exc))
            return

        for role, key in recommendation["keys"].items():
            apply_role(self.config, role, self.tool_db.by_key(key))
        self._tools_auto_applied = True
        self._refresh_widgets()
        self._schedule_autosave()

        note = recommendation["reasons"].get("isolation", "")
        prefix = f"copper clearance {gap:.3f} mm. " if gap is not None else ""
        self._tool_note = f"{prefix}{note}"
        self.status_label.setText(self._tool_note)
        self._debounce.start()

    def _build_isolation_card(self, layout: QVBoxLayout) -> None:
        card = Card("Isolation Milling", theme.ISOLATION)
        layout.addWidget(card)
        self.iso_enabled = self._bind_toggle(
            card, "Enable isolation",
            lambda: self.config.isolation_enabled,
            lambda v: setattr(self.config, "isolation_enabled", v),
        )
        self._bind_slider(
            card, "Depth", 0.01, 0.5,
            lambda: self.config.isolation_depth,
            lambda v: setattr(self.config, "isolation_depth", v),
            3, " mm",
        )
        self._bind_slider(
            card, "Passes", 1, 12,
            lambda: self.config.isolation_passes,
            lambda v: setattr(self.config, "isolation_passes", int(v)),
            0,
        )
        self._bind_slider(
            card, "Stepover", 0.0, 1.0,
            lambda: self.config.isolation_stepover,
            lambda v: setattr(self.config, "isolation_stepover", v),
            3, " mm",
        )
        self._bind_slider(
            card, "Clearance", 0.0, 0.3,
            lambda: self.config.isolation_clearance,
            lambda v: setattr(self.config, "isolation_clearance", v),
            3, " mm",
        )
        card.add_label("Stepover 0 uses an automatic 80 % overlap.")

    def _build_rubout_card(self, layout: QVBoxLayout) -> None:
        card = Card("Rub-Out / Copper Clearing", theme.RUBOUT, expanded=False)
        layout.addWidget(card)
        self._bind_toggle(
            card, "Enable rub-out",
            lambda: self.config.rubout_enabled,
            lambda v: setattr(self.config, "rubout_enabled", v),
        )
        self._bind_slider(
            card, "Depth", 0.01, 0.5,
            lambda: self.config.rubout_depth,
            lambda v: setattr(self.config, "rubout_depth", v),
            3, " mm",
        )
        self._bind_slider(
            card, "Stepover", 0.0, 2.0,
            lambda: self.config.rubout_stepover,
            lambda v: setattr(self.config, "rubout_stepover", v),
            3, " mm",
        )
        self._bind_slider(
            card, "Keep-out", 0.0, 1.0,
            lambda: self.config.rubout_clearance,
            lambda v: setattr(self.config, "rubout_clearance", v),
            3, " mm",
        )
        card.add_label("Rub-out is slow: expect long runtimes on dense boards.")

    def _build_cutout_card(self, layout: QVBoxLayout) -> None:
        card = Card("Board Cut-Out & Tabs", theme.CUTOUT)
        layout.addWidget(card)
        self._bind_toggle(
            card, "Enable cut-out",
            lambda: self.config.cutout_enabled,
            lambda v: setattr(self.config, "cutout_enabled", v),
        )
        self._bind_slider(
            card, "Depth", 0.1, 4.0,
            lambda: self.config.cutout_depth,
            lambda v: setattr(self.config, "cutout_depth", v),
            2, " mm",
        )
        self._bind_slider(
            card, "Stepdown", 0.05, 1.5,
            lambda: self.config.cutout_stepdown,
            lambda v: setattr(self.config, "cutout_stepdown", v),
            2, " mm",
        )
        self._bind_toggle(
            card, "Leave break-away tabs",
            lambda: self.config.tab_enabled,
            lambda v: setattr(self.config, "tab_enabled", v),
        )
        self._bind_slider(
            card, "Tab count", 0, 12,
            lambda: self.config.tab_count,
            lambda v: setattr(self.config, "tab_count", int(v)),
            0,
        )
        self._bind_slider(
            card, "Tab width", 0.2, 5.0,
            lambda: self.config.tab_width,
            lambda v: setattr(self.config, "tab_width", v),
            2, " mm",
        )

    def _build_drill_card(self, layout: QVBoxLayout) -> None:
        card = Card("Drilling", theme.DRILL)
        layout.addWidget(card)
        self._bind_toggle(
            card, "Enable drilling",
            lambda: self.config.drill_enabled,
            lambda v: setattr(self.config, "drill_enabled", v),
        )
        self._bind_slider(
            card, "Peck depth", 0.05, 2.0,
            lambda: self.config.peck_depth,
            lambda v: setattr(self.config, "peck_depth", v),
            2, " mm",
        )
        self._bind_slider(
            card, "Break-through", 0.0, 1.0,
            lambda: self.config.drill_depth_extra,
            lambda v: setattr(self.config, "drill_depth_extra", v),
            2, " mm",
        )
        self._bind_toggle(
            card, "Plated holes only",
            lambda: self.config.drill_only_plated,
            lambda v: setattr(self.config, "drill_only_plated", v),
        )

    def _build_silkscreen_card(self, layout: QVBoxLayout) -> None:
        card = Card("Silkscreen", theme.PINK, expanded=False)
        layout.addWidget(card)
        card.add_label(
            "Default mode assumes you apply the silkscreen by hand after the "
            "isolation, then the machine mills it back off the pads so they will "
            "take solder.",
            "Hint",
        )
        self._bind_toggle(
            card, "Silkscreen step",
            lambda: self.config.silkscreen_enabled,
            lambda v: setattr(self.config, "silkscreen_enabled", v),
        )

        self.silk_mode_combo = QComboBox()
        self.silk_mode_combo.addItem("Cut out of the pads", "cutout")
        self.silk_mode_combo.addItem("Engrave the artwork", "engrave")
        self.silk_mode_combo.currentIndexChanged.connect(
            lambda _: self._apply(
                lambda v: setattr(self.config, "silkscreen_mode", v),
                self.silk_mode_combo.currentData(),
            )
        )
        self.silk_mode_combo.setToolTip(
            "Cut out: mill the applied silkscreen off the pad openings.\n"
            "Engrave: mill an outline of the silkscreen artwork instead."
        )
        card.add_row("Mode", self.silk_mode_combo)

        self._bind_slider(
            card, "Depth", 0.02, 0.5,
            lambda: self.config.silkscreen_depth,
            lambda v: setattr(self.config, "silkscreen_depth", v),
            3, " mm",
        )
        self._bind_toggle(
            card, "Do both sides",
            lambda: self.config.silkscreen_bottom,
            lambda v: setattr(self.config, "silkscreen_bottom", v),
        ).setToolTip(
            "Run the silkscreen step on the back side as well, after the flip."
        )
        self._bind_toggle(
            card, "Clear the pad openings",
            lambda: self.config.silkscreen_clear_pads,
            lambda v: setattr(self.config, "silkscreen_clear_pads", v),
        ).setToolTip(
            "Engrave mode only: keeps the artwork off the exposed pads."
        )

    def _build_alignment_card(self, layout: QVBoxLayout) -> None:
        card = Card("Double-Sided Alignment", theme.PEACH, expanded=False)
        layout.addWidget(card)
        card.add_label(
            "Drills two deep pin holes on the flip axis. Because they sit on the "
            "axis, turning the board over leaves them in the same spot - so the "
            "back program is just a mirror of the front.",
            "Hint",
        )
        self._bind_toggle(
            card, "Drill alignment pins",
            lambda: self.config.alignment_holes,
            lambda v: setattr(self.config, "alignment_holes", v),
        )
        self._bind_slider(
            card, "Pin depth", 1.0, 20.0,
            lambda: self.config.alignment_hole_depth,
            lambda v: setattr(self.config, "alignment_hole_depth", v),
            1, " mm",
        ).setToolTip(
            "Goes through the blank and into the spoilboard so pins have something\n"
            "to seat in. 10 mm is a good default."
        )
        card.add_label(
            "Gets its own file, 01_alignment.gcode, and runs first. No tool change "
            "- it uses whatever bit is already in the spindle, and that bit's size "
            "is your pin size.",
            "Hint",
        )

        card.add_section_label("Pin positions (machine coordinates)")
        machine_x = self.profile.work_x
        machine_y = self.profile.work_y
        for label, axis, limit in (
            ("Pin 1  X", "alignment_pin1_x", machine_x),
            ("Pin 1  Y", "alignment_pin1_y", machine_y),
            ("Pin 2  X", "alignment_pin2_x", machine_x),
            ("Pin 2  Y", "alignment_pin2_y", machine_y),
        ):
            self._bind_slider(
                card, label, 0.0, float(limit),
                lambda a=axis: getattr(self.config, a),
                lambda v, a=axis: setattr(self.config, a, v),
                1, " mm",
            )
        card.add_label(
            "The flip axis is the line halfway between the two pins, and the "
            "board is centred on it automatically. Defaults give a pin at the "
            "origin and one 80 mm to the right.",
            "Hint",
        )

        self.flip_combo = QComboBox()
        self.flip_combo.addItem("End over end (horizontal axis)", "horizontal")
        self.flip_combo.addItem("Left to right (vertical axis)", "vertical")
        self.flip_combo.currentIndexChanged.connect(
            lambda _: self._apply(
                lambda v: setattr(self.config, "flip_axis", v),
                self.flip_combo.currentData(),
            )
        )
        self.flip_combo.setToolTip(
            "Only used when alignment pins are off. With pins on, the flip axis\n"
            "is set by the pins themselves."
        )
        card.add_row("Flip (no pins)", self.flip_combo)

    def _build_machine_card(self, layout: QVBoxLayout) -> None:
        card = Card("Machine & Feeds", theme.YELLOW, expanded=False)
        layout.addWidget(card)
        self._bind_slider(
            card, "Cut feed", 20.0, 1200.0,
            lambda: self.profile.cut_feed,
            lambda v: setattr(self.profile, "cut_feed", v),
            0, " mm/min",
        )
        self._bind_slider(
            card, "Plunge feed", 10.0, 600.0,
            lambda: self.profile.plunge_feed,
            lambda v: setattr(self.profile, "plunge_feed", v),
            0, " mm/min",
        )
        self._bind_slider(
            card, "Drill feed", 5.0, 400.0,
            lambda: self.profile.drill_feed,
            lambda v: setattr(self.profile, "drill_feed", v),
            0, " mm/min",
        )
        self._bind_slider(
            card, "Spindle", 10000, 15000,
            lambda: self.profile.spindle_rpm,
            lambda v: setattr(self.profile, "spindle_rpm", int(v)),
            0, " RPM",
        )
        self._bind_slider(
            card, "Rapid height", 0.2, 15.0,
            lambda: self.profile.rapid_z,
            lambda v: setattr(self.profile, "rapid_z", v),
            2, " mm",
        )
        self._bind_toggle(
            card, "Emit N-numbers",
            lambda: self.profile.line_numbers,
            lambda v: setattr(self.profile, "line_numbers", v),
        )
        self._bind_toggle(
            card, "Pause for tool changes",
            lambda: self.profile.use_tool_change_pause,
            lambda v: setattr(self.profile, "use_tool_change_pause", v),
        )
        self._bind_toggle(
            card, "Return to origin at end",
            lambda: self.profile.return_home_at_end,
            lambda v: setattr(self.profile, "return_home_at_end", v),
        )
        card.add_label(
            f"Envelope {self.profile.work_x:.0f} × {self.profile.work_y:.0f} "
            f"× {self.profile.work_z:.0f} mm"
        )

    def _build_view_card(self, layout: QVBoxLayout) -> None:
        card = Card("Preview Layers", theme.TEAL)
        layout.addWidget(card)

        self._plain_toggle(
            card, "Copper traces", True, self.viewer.set_copper_visible, "copper"
        )
        for kind, label in (
            ("isolation", "Isolation"),
            ("rubout", "Rub-out"),
            ("cutout", "Cut-out"),
        ):
            self._plain_toggle(
                card,
                label,
                True,
                lambda v, k=kind: self.viewer.set_visible(k, v),
                kind,
            )
        self._plain_toggle(
            card, "Drill holes", True, self.viewer.set_holes_visible, "holes"
        )
        self._plain_toggle(
            card, "Board outline", True, self.viewer.set_outline_visible, "outline"
        )
        self._plain_toggle(
            card, "Machine bed", True, self.viewer.set_bed_visible, "bed"
        )

        legend = self._plain_toggle(
            card, "Colour key", False, self._on_legend_toggled, "legend"
        )
        legend.setToolTip(
            "Overlay a key in the corner of the canvas.\n"
            "Off by default because a peck-drilled file lists every depth."
        )

        buttons = QWidget()
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        fit = QPushButton("Fit view")
        fit.clicked.connect(lambda: self.viewer.fit())
        row.addWidget(fit)
        row.addStretch(1)
        card.add(buttons)

    def _build_verify_card(self, layout: QVBoxLayout) -> None:
        card = Card("G-code Verification", theme.GREEN)
        layout.addWidget(card)
        card.add_label(
            "Every file is re-read and checked against the machine limits after it "
            "is generated. If this says all checks passed, the program is safe to run.",
            "Hint",
        )
        self.verify_headline = card.add_stat("Result", "not run", theme.OVERLAY)
        self.verify_stats = card.add_stat("Programs", "—")
        self.verify_distance = card.add_stat("Cutting distance", "—")
        self.verify_envelope = card.add_stat("Toolpath envelope", "—")
        self.verify_depth = card.add_stat("Deepest Z", "—")
        self.verify_time = card.add_stat("Estimated time", "—")

        self.verify_issues = QLabel("")
        self.verify_issues.setObjectName("Hint")
        self.verify_issues.setWordWrap(True)
        self.verify_issues.setStyleSheet(f"color: {theme.YELLOW};")
        self.verify_issues.setVisible(False)
        card.add(self.verify_issues)

        self.guide_button = QPushButton("Open the build guide")
        self.guide_button.setEnabled(False)
        self.guide_button.setToolTip(
            "Opens the README.md written next to the G-code files"
        )
        self.guide_button.clicked.connect(self._open_guide)
        card.add(self.guide_button)

    def _build_report_card(self, layout: QVBoxLayout) -> None:
        card = Card("Layer Report", theme.SUBTEXT, expanded=False)
        layout.addWidget(card)
        self.report_container = QWidget()
        self.report_layout = QVBoxLayout(self.report_container)
        self.report_layout.setContentsMargins(0, 0, 0, 0)
        self.report_layout.setSpacing(3)
        card.add(self.report_container)


    def _apply(self, setter, value) -> None:
        setter(value)
        self._update_enabled_states()
        self._update_derived()
        self._debounce.start()
        self._schedule_autosave()


    def _schedule_autosave(self) -> None:
        self._autosave.start()

    def _save_preferences(self) -> None:
        self.preferences.save_job(self.config, self.profile)
        self.preferences.save_viewer(self.viewer.visibility())
        self.preferences.save_panel_hidden(self._panel_hidden)
        if self.archive is not None:
            self.preferences.save_archive(str(self.archive))

    def _sync_viewer_toggles(self) -> None:
        state = self.viewer.visibility()
        layers = state.get("layers") or {}
        for key, row in self.viewer_toggles.items():
            if key in layers:
                row.setChecked(bool(layers[key]))
            elif key in state:
                row.setChecked(bool(state[key]))

    def _install_shortcuts(self) -> None:
        for keys, slot in (
            ("Ctrl+O", self._choose_archive),
            ("Ctrl+E", self._export),
            ("Ctrl+R", self._request_plan),
            ("Ctrl+F", lambda: self.viewer.fit()),
            ("Ctrl+0", lambda: self.viewer.fit()),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(keys))
            action.triggered.connect(slot)
            self.addAction(action)

    def _refresh_widgets(self) -> None:
        for widget, getter, _setter in self._bindings:
            if isinstance(widget, SliderRow):
                widget.set_value(getter())
            elif isinstance(widget, ToggleRow):
                widget.setChecked(bool(getter()))
        index = self.origin_combo.findData(self.config.origin_mode)
        if index >= 0:
            self.origin_combo.blockSignals(True)
            self.origin_combo.setCurrentIndex(index)
            self.origin_combo.blockSignals(False)
        for combo, value in (
            (self.tool_kind_combo, self.config.isolation_tool.kind),
            (self.flip_combo, self.config.flip_axis),
            (self.silk_mode_combo, self.config.silkscreen_mode),
        ):
            index = combo.findData(value)
            if index >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)
        self._update_enabled_states()
        self._update_derived()
        self._sync_tool_combos()

    def _update_enabled_states(self) -> None:
        is_vbit = self.config.isolation_tool.kind == "vbit"
        self.angle_row.set_enabled(is_vbit)
        self.tip_row.set_enabled(is_vbit)
        self.tool_diameter_row.set_enabled(not is_vbit)
        self.subtitle.setText(
            f"{self.profile.name}   ·   {self.config.origin_mode.replace('_', '-')}"
        )
        self._update_side_hint()

    def _update_derived(self) -> None:
        tool = self.config.isolation_tool
        if tool.kind == "vbit":
            radius = tool.tip_diameter / 2.0 + self.config.isolation_depth * math.tan(
                math.radians(tool.angle / 2.0)
            )
        else:
            radius = tool.diameter / 2.0
        self.cut_width_stat.set_value(f"{radius * 2.0:.3f} mm")

    def _on_tool_kind_changed(self, _index: int) -> None:
        self.config.isolation_tool.kind = self.tool_kind_combo.currentData()
        self._update_enabled_states()
        self._update_derived()
        self._debounce.start()

    def _on_cursor_moved(self, _x: float, _y: float) -> None:
        pass

    def _check_for_updates(self) -> None:
        thread = threading.Thread(target=self._run_update_check, daemon=True)
        thread.start()

    def _run_update_check(self) -> None:
        info = check_for_update()
        if info is None:
            return
        self._update_info = info
        QTimer.singleShot(0, self._show_update_banner)

    def _show_update_banner(self) -> None:
        info = self._update_info
        if info is None:
            return
        self.update_label.setText(
            f"Update available: version {info.version} (you have {info.current})."
        )
        self.update_strip.setVisible(True)


    def _choose_archive(self) -> None:
        start = str(self.archive.parent) if self.archive else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Gerber archive", start, "Gerber archive (*.zip);;All files (*)"
        )
        if path:
            self._load(Path(path))

    def _reload(self) -> None:
        if self.archive:
            self._load(self.archive)

    def _load(self, path: Path) -> None:
        self.status_label.setText(f"Parsing {path.name}…")
        try:
            if self.project is not None:
                self.project.cleanup()
            self.project = load_project(path)
        except Exception as exc:
            self.project = None
            self.status_label.setText(f"Error: {exc}")
            return

        self.archive = path
        self.viewer.clear()

        has_back = self.project.layer(LayerType.BOTTOM_COPPER) is not None
        if has_back and self._sides_defaulted_for != str(path):
            self.config.mill_bottom = True
        self._sides_defaulted_for = str(path)
        self._tools_auto_applied = False
        self._auto_select_tools()
        self._refresh_widgets()
        self.preferences.save_archive(str(path))
        self._schedule_autosave()

        self._populate_report(self.project)
        note = getattr(self, "_tool_note", "")
        self.status_label.setText(
            f"Loaded {path.name} — {note}" if note else f"Loaded {path.name} — computing…"
        )
        self._request_plan()

    def _update_side_hint(self) -> None:
        project = self.project
        if project is None:
            self.sides_stat.set_value("—", theme.OVERLAY)
            self.sides_hint.setVisible(False)
            return

        sides: list[str] = []
        if project.layer(LayerType.TOP_COPPER) is not None:
            sides.append("front")
        if project.layer(LayerType.BOTTOM_COPPER) is not None:
            sides.append("back")

        self.sides_stat.set_value(
            " + ".join(sides) if sides else "none",
            theme.TEXT if sides else theme.RED,
        )

        problems: list[str] = []
        if "back" in sides and not self.config.mill_bottom:
            problems.append(
                "This Gerber has a back copper layer but 'Back copper (bottom)' is "
                "off, so only the front is being milled. Tick it to get the second "
                "isolation file."
            )
        if "back" in sides and self.config.mill_bottom and not self.config.silkscreen_enabled:
            problems.append(
                "Silkscreen is off, so no silkscreen_cutout files are produced. "
                "Turn on 'Silkscreen step' if you want them."
            )

        self.sides_hint.setText("  ".join(problems))
        self.sides_hint.setVisible(bool(problems))

    def _request_plan(self) -> None:
        if self.project is None:
            return
        self._debounce.stop()
        self.progress.setVisible(True)
        self.recompute_button.setEnabled(False)
        self.planner.request(self.project, self.config, self.profile)

    def _update_verify_card(self, report) -> None:
        if report is None:
            self.verify_headline.set_value("not run", theme.OVERLAY)
            return
        errors = len(report.errors)
        warnings = len(report.warnings)
        colour = theme.RED if errors else (theme.YELLOW if warnings else theme.GREEN)
        self.verify_headline.set_value(report.headline(), colour)

        stats = report.stats
        self.verify_stats.set_value(str(stats.get("files", 1)))
        self.verify_distance.set_value(f"{stats.get('cut_distance', 0):.0f} mm")
        bounds = stats.get("xy_bounds")
        if bounds:
            self.verify_envelope.set_value(
                f"{bounds[2] - bounds[0]:.1f} × {bounds[3] - bounds[1]:.1f} mm"
            )
        self.verify_depth.set_value(f"{stats.get('z_min', 0):.3f} mm")
        self.verify_time.set_value(
            _fmt_minutes(stats.get("estimated_minutes", 0.0))
        )

        if report.issues:
            lines = [f"• {issue.message}" for issue in report.issues[:6]]
            if len(report.issues) > 6:
                lines.append(f"• …and {len(report.issues) - 6} more")
            self.verify_issues.setText("\n".join(lines))
            self.verify_issues.setVisible(True)
        else:
            self.verify_issues.setVisible(False)

    def _on_plan_ready(self, plan, report) -> None:
        self.plan = plan
        self.verification = report
        self.progress.setVisible(False)
        self.recompute_button.setEnabled(True)
        self._update_verify_card(report)

        if not self._gcode_mode:
            self.viewer.set_plan(
                plan, (self.profile.work_x, self.profile.work_y)
            )

        runtime = estimate_runtime(plan, self.profile, self.config)
        paths = sum(len(g.polylines) for g in plan.groups)
        holes = sum(len(g.holes) for g in plan.groups)
        self.stat_runtime[1].setText(runtime)
        self.stat_cut[1].setText(f"{plan.total_cut_length:.0f} mm")
        self.stat_paths[1].setText(str(paths))
        self.stat_holes[1].setText(str(holes))

        self.status_label.setText(
            f"Ready — {len(plan.groups)} operations, estimated {runtime}"
        )

        warnings = list(plan.warnings)
        if warnings:
            self.warning_label.setText(
                f"{len(warnings)} notice(s): " + "; ".join(warnings[:2])
            )
            self.warning_strip.setVisible(True)
        else:
            self.warning_strip.setVisible(False)

        board = plan.board
        fits = board.width <= self.profile.work_x and board.height <= self.profile.work_y
        self.fit_stat.set_value(
            f"{board.width:.1f} × {board.height:.1f} mm "
            + ("fits" if fits else "TOO BIG"),
            theme.GREEN if fits else theme.RED,
        )

    def _on_plan_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.recompute_button.setEnabled(True)
        self.status_label.setText(f"Error: {message}")

    def _populate_report(self, project: PcbProject) -> None:
        while self.report_layout.count():
            item = self.report_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for layer in project.layers:
            colour = {
                LayerType.TOP_COPPER: theme.COPPER_TOP,
                LayerType.BOTTOM_COPPER: theme.COPPER_BOTTOM,
                LayerType.EDGE_CUTS: theme.OUTLINE,
            }.get(layer.layer_type, theme.SUBTEXT)
            row = StatRow(layer.name[:30], layer.layer_type.display_name, colour)
            self.report_layout.addWidget(row)

        for drill in project.drills:
            sizes = " ".join(
                f"{d:.3f}×{len(h)}" for d, h in drill.by_diameter().items()
            )
            row = StatRow(drill.name[:30], f"{len(drill.holes)} holes  {sizes}", theme.DRILL)
            self.report_layout.addWidget(row)


    def _apply_isolation_preset(self) -> None:
        self.config.isolation_enabled = True
        self.config.isolation_passes = 1
        self.config.isolation_depth = 0.05
        self.config.isolation_stepover = 0.0
        self.config.isolation_clearance = 0.0
        self.config.rubout_enabled = False
        self.config.drill_enabled = True
        self.config.cutout_enabled = True
        self.config.tab_enabled = True
        self.config.mill_top = True
        self.config.mill_bottom = False
        self._refresh_widgets()
        self._request_plan()

    def _apply_quality_preset(self) -> None:
        self._apply_isolation_preset()
        self.config.isolation_passes = 3
        self.config.rubout_enabled = True
        self._refresh_widgets()
        self._request_plan()

    def _one_click(self) -> None:
        self._apply_isolation_preset()
        if self.plan is not None:
            self._export()


    def _export(self) -> None:
        if self.plan is None:
            return
        stem = self.archive.stem if self.archive else "board"
        parent = self.archive.parent if self.archive else Path.cwd()
        start = str(parent if parent.exists() else Path.cwd())

        directory = QFileDialog.getExistingDirectory(
            self, "Choose where to write the G-code package", start
        )
        if not directory:
            return

        out_dir = Path(directory) / f"{stem}_milling"
        try:
            package = write_package(
                self.plan, self.config, self.profile, out_dir, stem
            )
        except Exception as exc:
            self.status_label.setText(f"Export failed: {exc}")
            return

        self.package = package
        self.guide_button.setEnabled(package.readme is not None)
        self._update_verify_card(package.verification)

        errors = len(package.verification.errors) if package.verification else 0
        self.status_label.setText(
            f"Wrote {len(package.files)} file(s) + README to {out_dir}"
            + (f"  —  {errors} verification error(s)!" if errors else "")
        )

    def _open_guide(self) -> None:
        if self.package is None or self.package.readme is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.package.readme)))


    def _open_gcode(self) -> None:
        start = (
            str(self.package.directory)
            if self.package is not None
            else str(self.archive.parent if self.archive else Path.cwd())
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open G-code",
            start,
            "G-code (*.gcode *.nc *.tap *.txt);;All files (*)",
        )
        if not path:
            return

        try:
            program = read_gcode_file(path)
        except Exception as exc:
            self.status_label.setText(f"Could not read {Path(path).name}: {exc}")
            return
        if not program.segments:
            self.status_label.setText(f"{Path(path).name} contains no motion.")
            return

        self.program = program
        self._enter_gcode_mode()
        stats = program.stats
        self.status_label.setText(
            f"{program.name}: {stats['lines']} lines, "
            f"{stats['cut_distance']:.0f} mm of cutting, "
            f"{stats['tool_changes']} tool change(s), "
            f"{_fmt_minutes(stats['estimated_minutes'])}"
        )

    def _enter_gcode_mode(self) -> None:
        if self.program is None:
            return
        self._gcode_mode = True
        self._play_position = 1.0
        self.scrubber.blockSignals(True)
        self.scrubber.setValue(self.scrubber.maximum())
        self.scrubber.blockSignals(False)
        self.viewer.set_program(
            self.program, (self.profile.work_x, self.profile.work_y)
        )
        self.viewer.fit()
        self.transport.setVisible(True)
        self._update_transport()

    def _exit_gcode_mode(self) -> None:
        self._stop_playback()
        self._gcode_mode = False
        self.transport.setVisible(False)
        self.viewer.clear_program()
        if self.plan is not None:
            self.viewer.set_plan(
                self.plan, (self.profile.work_x, self.profile.work_y)
            )

    def _toggle_playback(self) -> None:
        if self._play_timer.isActive():
            self._stop_playback()
            return
        if self.program is None:
            return
        if self._play_position >= 1.0:
            self._play_position = 0.0
        self._play_timer.start()
        self.play_button.setText("Pause")

    def _stop_playback(self) -> None:
        self._play_timer.stop()
        self.play_button.setText("Play")

    def _advance_playback(self) -> None:
        step = (self._play_timer.interval() / 1000.0) / PLAYBACK_SECONDS
        self._play_position = min(1.0, self._play_position + step)
        self.scrubber.blockSignals(True)
        self.scrubber.setValue(int(self._play_position * self.scrubber.maximum()))
        self.scrubber.blockSignals(False)
        self._apply_progress(self._play_position)
        if self._play_position >= 1.0:
            self._stop_playback()

    def _on_scrub(self, value: int) -> None:
        if self.program is None:
            return
        self._stop_playback()
        self._play_position = value / max(1, self.scrubber.maximum())
        self._apply_progress(self._play_position)

    def _apply_progress(self, fraction: float) -> None:
        self.viewer.set_progress(fraction)
        program = self.program
        if program is None or not program.segments:
            return
        index = min(
            len(program.segments) - 1, int(fraction * len(program.segments))
        )
        segment = program.segments[index]
        self.transport_readout.setText(
            f"line {segment.line} / {program.stats['lines']}"
            f"   X {segment.x1:.2f}  Y {segment.y1:.2f}  Z {segment.z1:.2f}"
        )

    def _on_rapids_toggled(self, visible: bool) -> None:
        self.viewer.show_rapids = visible
        self.viewer.refresh()
        self._schedule_autosave()

    def _on_pauses_toggled(self, visible: bool) -> None:
        self.viewer.show_program_pauses = visible
        self.viewer.refresh()
        self._schedule_autosave()

    def _on_legend_toggled(self, visible: bool) -> None:
        self.viewer.show_legend = visible
        self.viewer.refresh()
        self._schedule_autosave()

    def _update_transport(self) -> None:
        program = self.program
        if program is None:
            return
        depths = program.stats.get("depths") or []
        if len(depths) > 1:
            depth_text = f"{depths[0]:.2f} to {depths[-1]:.2f} mm"
        elif depths:
            depth_text = f"{depths[0]:.2f} mm"
        else:
            depth_text = "no cutting moves"
        self.transport_label.setText(f"{program.name}  ·  {depth_text}")
        self._apply_progress(self._play_position)


    def closeEvent(self, event) -> None:
        self._autosave.stop()
        self._save_preferences()
        self.preferences.save_window(self)

        try:
            self.planner.finished.disconnect()
            self.planner.failed.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.planner.shutdown()

        if self.project is not None:
            self.project.cleanup()
            self.project = None
        super().closeEvent(event)


def _fmt_minutes(minutes: float) -> str:
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours} h {mins:02d} min"
    return f"{mins} min"


def _vline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFixedWidth(1)
    line.setStyleSheet(f"background-color: {theme.SURFACE0}; border: none;")
    return line


def _status_stat(label: str, colour: str) -> tuple[QLabel, QLabel]:
    key = QLabel(label)
    key.setObjectName("StatusKey")
    value = QLabel("—")
    value.setObjectName("StatusValue")
    value.setStyleSheet(f"color: {colour};")
    return key, value
