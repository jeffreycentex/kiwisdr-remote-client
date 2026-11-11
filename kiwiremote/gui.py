# SPDX-License-Identifier: GPL-3.0-or-later
"""PySide6 main window. All actions go through KiwiService, so changes made by
the HTTP API or rigctld show up here (via events) exactly like local ones."""

import logging
import math
import os
import shutil
import sys
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QStandardPaths, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QFont, QFontDatabase, QKeySequence, QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSlider, QSpinBox,
    QScrollArea, QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView, QStatusBar, QVBoxLayout, QWidget)

from . import __version__
from .api import DEFAULT_PORT as API_DEFAULT_PORT, ApiServer
from .dsp import DEFAULT_AUDIO_BANDPASS, DEFAULT_AUDIO_NOTCH, HAVE_SCIPY
from .digital import DATA_MODES
from .rigctld import RigctldServer
from .service import ApiError, KiwiService, SessionPolicy, default_record_dir
from .sinks import (HAVE_PYAV, HAVE_SOUNDDEVICE, PULSE_PREFIX, create_virtual_cable,
                    default_recording_name, guess_virtual_cable, list_output_devices,
                    sounddevice_error)
from .state import DEFAULT_NOISE_BLANKER, DEFAULT_PASSBAND, MIN_FREQ_HZ, MODES, ServerSpec

log = logging.getLogger(__name__)

PUBLIC_LIST_URL = "http://rx.kiwisdr.com/"
STEPS_HZ = [10, 100, 500, 1000, 5000, 9000, 10000, 100000]
MAX_HISTORY = 15
MAX_PRESETS = 15
MAX_PRESET_FILE_BYTES = 16384
MAX_LOG_LINES = 2000
PRESET_HEADER = (
    '# KiwiSDR Remote quick-tune buttons: one per line, "name, frequency in kHz".\n'
    '# Up to 15 buttons; names up to 10 characters. Lines starting with # are ignored.\n'
    '# Saved changes show up in the running app. Delete this file to hide the buttons.\n'
    '# 40m FT8, 7074\n'
)


def s_units(dbm):
    """dBm -> 'S7', 'S9+20' (HF convention: S9 = -73 dBm, 6 dB per S-unit)."""
    if dbm >= -73:
        over = int(round(dbm + 73))
        return "S9+%d" % over if over >= 1 else "S9"
    s = max(0, 9 + int(round((dbm + 73) / 6)))
    return "S%d" % s


class _Bridge(QObject):
    """Carries events from worker/rigctld threads to the GUI thread."""
    event = Signal(str, object)


class _LogBuffer(logging.Handler):
    """Buffer worker logs for the GUI timer without posting one Qt event per record."""

    def __init__(self):
        super().__init__()
        self.pending = deque(maxlen=MAX_LOG_LINES)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                            datefmt="%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        self.pending.append(self.format(record))

    def drain(self):
        with self.lock:
            batch = "\n".join(self.pending)
            self.pending.clear()
        return batch


def apply_light_theme(app):
    """Use a consistent light palette even when the desktop uses dark mode."""
    app.setStyle("Fusion")
    if hasattr(app.styleHints(), "setColorScheme"):
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    palette = QPalette()
    for role, color in (
        (QPalette.Window, "#f6f7f9"), (QPalette.WindowText, "#172334"),
        (QPalette.Base, "#ffffff"), (QPalette.AlternateBase, "#eef2f7"),
        (QPalette.Text, "#172334"), (QPalette.Button, "#ffffff"),
        (QPalette.ButtonText, "#172334"), (QPalette.ToolTipBase, "#ffffff"),
        (QPalette.ToolTipText, "#172334"), (QPalette.Highlight, "#2563eb"),
        (QPalette.HighlightedText, "#ffffff"), (QPalette.Mid, "#cbd3df"),
        (QPalette.Light, "#ffffff"), (QPalette.Dark, "#9ca9b9"),
        (QPalette.PlaceholderText, "#64748b"),
    ):
        palette.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor("#8993a2"))
    app.setPalette(palette)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        apply_light_theme(QApplication.instance())
        self.setWindowTitle("KiwiSDR Remote %s" % __version__)
        self.settings = QSettings("KiwiSDR-Remote", "KiwiSDR Remote")
        self._preset_path = Path(QStandardPaths.writableLocation(
            QStandardPaths.AppConfigLocation)) / "quicktune.txt"
        self._preset_file = None
        self.bridge = _Bridge()
        self.bridge.event.connect(self._on_event, Qt.QueuedConnection)
        emit = self.bridge.event.emit
        self.service = KiwiService(on_event=emit,
                                   policy=self._load_policy(),
                                   compression=self.settings.value("compression", True, type=bool))
        self.radio = self.service.radio
        self.rigctld = None
        self.api = None
        self._rec_started = None
        self._data_session = 0
        self._data_active = False
        self._flamp_transfers = []

        self.resize(680, 600)
        self.setMinimumSize(560, 460)
        self.setStyleSheet("""
            QGroupBox { font-weight: 600; border: 1px solid palette(mid);
                        border-radius: 6px; margin-top: 8px; padding: 9px 7px 5px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QPushButton { min-height: 22px; padding: 2px 9px; }
            QLineEdit, QComboBox, QSpinBox { min-height: 22px; }
            QPushButton#connectAction { background: #2563eb; color: white;
                                       border: 0; border-radius: 5px; font-weight: 600; }
            QPushButton#recordAction:checked { background: #b42332; color: white;
                                              border-radius: 5px; }
        """)
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 6)
        root.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel("KiwiSDR Remote")
        font = QFont(title.font())
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        title.setFont(font)
        header.addWidget(title)
        header.addStretch()
        self.state_badge = QLabel("Idle")
        self.state_badge.setAccessibleName("Receiver connection state")
        self.state_badge.setMinimumWidth(110)
        self.state_badge.setAlignment(Qt.AlignCenter)
        header.addWidget(self.state_badge)
        root.addLayout(header)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        for title, builders in (
            ("Listen", (self._build_server_box, self._build_tune_box, self._build_quick_tune)),
            ("Audio", (self._build_audio_box, self._build_record_box)),
            ("DSP", (self._build_dsp_box, self._build_audio_bandpass,
                     self._build_manual_notch, self._build_receiver_filters)),
            ("Automation", (self._build_rigctl_box, self._build_api_box)),
        ):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setSpacing(6)
            for builder in builders:
                layout.addWidget(builder())
            layout.addStretch()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.NoFrame)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, title)
        self.tabs.addTab(self._build_data_tab(), "Data")
        self.tabs.addTab(self._build_flamp_tab(), "FLAMP")
        self.tabs.addTab(self._build_log_tab(), "Log")
        self.menuBar().addMenu("&File").addAction("Edit quick-tune presets…", self._edit_quick_tune)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready · Select a receiver to begin")
        self._shortcuts = []
        for title, keys, callback in (
            ("Focus receiver", "Ctrl+L", lambda: self._focus_control(self.server_combo)),
            ("Focus frequency", "Ctrl+F", lambda: self._focus_control(self.freq)),
            ("Start or stop recording", "Ctrl+R", self.rec_btn.click),
            ("Connect or disconnect", "Ctrl+Return", self.connect_btn.click),
        ):
            action = QAction(title, self)
            action.setShortcut(QKeySequence(keys))
            action.triggered.connect(callback)
            self.addAction(action)
            self._shortcuts.append(action)

        self._log_buffer = _LogBuffer()
        logging.getLogger().addHandler(self._log_buffer)
        self._load_settings()
        self._reload_quick_tune()
        self._timer = QTimer(self, interval=500, timeout=self._tick)
        self._timer.start()

        if not HAVE_SOUNDDEVICE:
            self.audio_enable.setEnabled(False)
            self.audio_enable.setToolTip("PortAudio not available: %s" % sounddevice_error())
        if self.rig_enable.isChecked():
            self._toggle_rigctld(True)
        if self.api_enable.isChecked():
            self._toggle_api(True)

    def _focus_control(self, widget):
        self.tabs.setCurrentIndex(0)
        self.tabs.widget(0).ensureWidgetVisible(widget)
        widget.setFocus()
        if hasattr(widget, "selectAll"):
            widget.selectAll()
        elif isinstance(widget, QComboBox):
            widget.lineEdit().selectAll()

    # ================================================================== layout
    def _build_server_box(self):
        box = QGroupBox("KiwiSDR server")
        g = QGridLayout(box)
        self.server_combo = QComboBox(editable=True)
        self.server_combo.setInsertPolicy(QComboBox.NoInsert)
        self.server_combo.lineEdit().setPlaceholderText("host:port   e.g. mykiwi.example.net:8073")
        self.server_combo.lineEdit().returnPressed.connect(self._connect_clicked)
        self.server_combo.textActivated.connect(self._server_picked)
        self.password = QLineEdit(echoMode=QLineEdit.Password, placeholderText="(optional)")
        self.user_name = QLineEdit(placeholderText="shown in the Kiwi user list")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("connectAction")
        self.connect_btn.setToolTip("Connect / disconnect (Ctrl+Return; Command+Return on macOS)")
        self.connect_btn.clicked.connect(self._connect_clicked)
        find_btn = QPushButton("Find receivers…")
        find_btn.setToolTip(PUBLIC_LIST_URL)
        find_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(PUBLIC_LIST_URL)))
        self.compression = QCheckBox("Compressed audio")
        self.compression.setToolTip("ADPCM uses about one quarter of the uncompressed bandwidth")
        self.tls = QCheckBox("TLS (https)")
        self.tls.setToolTip("For Kiwis served over https. Without a port, 443 is used.\n"
                            "An https:// address turns this on by itself.")
        self.conn_label = QLabel("Enter a receiver address, then choose Connect.")
        self.conn_label.setWordWrap(True)
        self.session_label = QLabel("")
        self.session_label.setWordWrap(True)
        self.session_label.setStyleSheet("color: gray")
        self.smeter = QProgressBar(minimum=-127, maximum=-13, textVisible=True)
        self.smeter.setFormat("—")
        self.smeter.setValue(-127)

        g.addWidget(QLabel("Server"), 0, 0)
        g.addWidget(self.server_combo, 0, 1, 1, 2)
        g.addWidget(self.connect_btn, 0, 3)
        g.addWidget(QLabel("Password"), 1, 0)
        g.addWidget(self.password, 1, 1)
        g.addWidget(QLabel("Name"), 1, 2)
        g.addWidget(self.user_name, 1, 3)
        opts = QHBoxLayout()
        opts.addWidget(self.compression)
        opts.addWidget(self.tls)
        opts.addStretch(1)
        g.addLayout(opts, 2, 1, 1, 2)
        g.addWidget(find_btn, 2, 3)
        g.addWidget(self.conn_label, 3, 0, 1, 4)
        g.addWidget(self.session_label, 4, 0, 1, 4)
        g.addWidget(self.smeter, 5, 0, 1, 4)
        g.setColumnStretch(1, 1)
        return box

    def _build_tune_box(self):
        box = QGroupBox("Tuning")
        v = QVBoxLayout(box)

        row = QHBoxLayout()
        self.freq = QDoubleSpinBox()
        self.freq.setDecimals(3)
        self.freq.setRange(1.0, 32000.0)
        self.freq.setSuffix(" kHz")
        self.freq.setKeyboardTracking(False)          # commit on Enter / arrows only
        self.freq.setAccelerated(True)
        f = QFont(self.freq.font())
        f.setPointSize(f.pointSize() + 5)
        f.setBold(True)
        self.freq.setFont(f)
        self.freq.setMinimumWidth(260)
        self.freq.valueChanged.connect(self._freq_edited)
        self.step = QComboBox()
        for s in STEPS_HZ:
            self.step.addItem(("%d Hz" % s) if s < 1000 else ("%g kHz" % (s / 1000)), s)
        self.step.currentIndexChanged.connect(self._step_changed)
        row.addWidget(self.freq, 1)
        row.addWidget(QLabel("Step"))
        row.addWidget(self.step)
        v.addLayout(row)

        row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for name in MODES:
            b = QRadioButton(name)
            self.mode_group.addButton(b)
            self.mode_buttons[name] = b
            row.addWidget(b)
        self.mode_group.buttonClicked.connect(self._mode_clicked)
        row.addSpacing(20)
        row.addWidget(QLabel("Bandwidth"))
        self.passband = QSpinBox(minimum=100, maximum=12000, singleStep=100, suffix=" Hz")
        self.passband.setKeyboardTracking(False)
        self.passband.valueChanged.connect(self._passband_edited)
        row.addWidget(self.passband)
        row.addStretch(1)
        v.addLayout(row)
        return box

    def _build_quick_tune(self):
        self.quick_tune_box = QGroupBox("Quick Tune")
        layout = QGridLayout(self.quick_tune_box)
        self.quick_tune_buttons = []
        for index in range(MAX_PRESETS):
            button = QPushButton()
            button.clicked.connect(lambda checked=False, b=button: self._freq_edited(b.property("khz")))
            layout.addWidget(button, index // 5, index % 5)
            button.hide()
            self.quick_tune_buttons.append(button)
        self.quick_tune_box.hide()
        return self.quick_tune_box

    def _edit_quick_tune(self):
        try:
            self._preset_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self._preset_path.open("x", encoding="utf-8") as presets:
                    presets.write(PRESET_HEADER)
            except FileExistsError:
                pass
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._preset_path))):
                raise OSError("No editor opened %s" % self._preset_path)
        except OSError as e:
            QMessageBox.warning(self, "Quick Tune", str(e))

    def _reload_quick_tune(self):
        try:
            with self._preset_path.open("rb") as presets:
                contents = presets.read(MAX_PRESET_FILE_BYTES + 1)
        except FileNotFoundError:
            contents = b""
        except OSError:
            return
        if contents == self._preset_file:
            return
        self._preset_file = contents
        if len(contents) > MAX_PRESET_FILE_BYTES:
            log.warning("quicktune.txt exceeds %d bytes", MAX_PRESET_FILE_BYTES)
            return

        entries = []
        for number, line in enumerate(contents.decode("utf-8-sig", errors="replace").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            label, separator, freq = line.rpartition(",")
            label = label.strip()
            try:
                khz = float(freq)
                if not separator or not label or not math.isfinite(khz) or khz < MIN_FREQ_HZ / 1000:
                    raise ValueError
            except ValueError:
                log.warning("quicktune.txt line %d: expected a name and frequency in kHz", number)
                continue
            entries.append((label[:10], khz))
            if len(entries) == MAX_PRESETS:
                break

        for button, (label, khz) in zip(self.quick_tune_buttons, entries):
            button.setText(label.replace("&", "&&"))
            button.setProperty("khz", khz)
            button.setToolTip("%s · %g kHz" % (label, khz))
            button.show()
        for button in self.quick_tune_buttons[len(entries):]:
            button.hide()
        self.quick_tune_box.setVisible(bool(entries))

    def _build_dsp_box(self):
        box = QGroupBox("Noise Blanker")
        layout = QFormLayout(box)
        enabled, gate_us, threshold = DEFAULT_NOISE_BLANKER
        self.nb_enabled = QCheckBox("Enable noise blanker", checked=enabled)
        self.nb_gate = QSpinBox(minimum=100, maximum=5000, singleStep=100,
                                value=gate_us, suffix=" µs")
        self.nb_threshold = QSpinBox(minimum=0, maximum=100, value=threshold, suffix=" %")
        layout.addRow(self.nb_enabled)
        layout.addRow(self._dsp_settings_row(("Gate time", self.nb_gate),
                                             ("Threshold", self.nb_threshold)))
        return box

    def _dsp_settings_row(self, *fields):
        row = QHBoxLayout()
        for name, spin in fields:
            spin.setKeyboardTracking(False)
            label = QLabel(name)
            label.setBuddy(spin)
            row.addWidget(label)
            row.addWidget(spin)
        row.addStretch()
        return row

    def _apply_noise_blanker(self, *_):
        self.service.set_noise_blanker(self.nb_enabled.isChecked(), self.nb_gate.value(),
                                       self.nb_threshold.value())

    def _build_audio_bandpass(self):
        box, self.bandpass_enabled, self.bandpass_center, self.bandpass_width = (
            self._build_audio_filter("Audio Bandpass", "Center", DEFAULT_AUDIO_BANDPASS, 50))
        return box

    def _build_manual_notch(self):
        box, self.notch_enabled, self.notch_frequency, self.notch_width = (
            self._build_audio_filter("Manual Notch", "Frequency", DEFAULT_AUDIO_NOTCH, 10))
        return box

    def _build_audio_filter(self, title, frequency_label, defaults, step):
        box = QGroupBox(title)
        layout = QFormLayout(box)
        enabled, center, width = defaults
        checkbox = QCheckBox("Enable " + title.lower(), checked=enabled)
        frequency = QSpinBox(minimum=1, maximum=100000, singleStep=step,
                             value=center, suffix=" Hz")
        bandwidth = QSpinBox(minimum=1, maximum=100000, singleStep=step,
                             value=width, suffix=" Hz")
        layout.addRow(checkbox)
        layout.addRow(self._dsp_settings_row((frequency_label, frequency), ("Width", bandwidth)))
        box.setToolTip("Filters playback and recordings." if HAVE_SCIPY else
                       "Install SciPy to use this filter.")
        box.setEnabled(HAVE_SCIPY)
        return box, checkbox, frequency, bandwidth

    def _build_receiver_filters(self):
        box = QGroupBox("KiwiSDR Noise Filters")
        row = QHBoxLayout(box)
        self.auto_notch = QCheckBox("Auto Notch")
        self.auto_notch.setToolTip("Suppresses steady or drifting tones. Leave off for digital decoding.")
        self.noise_reduction = QCheckBox("Noise Reduction")
        self.noise_reduction.setToolTip("Reduces background noise for voice listening. Leave off for digital decoding.")
        row.addWidget(self.auto_notch)
        row.addWidget(self.noise_reduction)
        row.addStretch()
        return box

    def _apply_audio_bandpass(self, *_):
        self.service.set_audio_filter(HAVE_SCIPY and self.bandpass_enabled.isChecked(),
                                      self.bandpass_center.value(), self.bandpass_width.value())

    def _apply_manual_notch(self, *_):
        self.service.set_audio_filter(HAVE_SCIPY and self.notch_enabled.isChecked(),
                                      self.notch_frequency.value(), self.notch_width.value(),
                                      notch=True)

    def _apply_receiver_filters(self, *_):
        self.service.set_noise_filters(self.noise_reduction.isChecked(), self.auto_notch.isChecked())

    def _build_log_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.log_view = QPlainTextEdit(readOnly=True, maximumBlockCount=MAX_LOG_LINES)
        self.log_view.setAccessibleName("Application log")
        layout.addWidget(self.log_view)
        export = QPushButton("Export log…")
        export.clicked.connect(self._export_log)
        layout.addWidget(export, alignment=Qt.AlignRight)
        return page

    def _build_data_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.data_mode = QComboBox()
        self.data_mode.addItems(list(DATA_MODES))
        self.data_mode.setAccessibleName("Digital mode")
        mode_label = QLabel("Mode")
        mode_label.setBuddy(self.data_mode)
        controls.addWidget(mode_label)
        controls.addWidget(self.data_mode)
        self.data_sweetspot = QDoubleSpinBox(decimals=1, singleStep=25, suffix=" Hz")
        self.data_sweetspot.setKeyboardTracking(False)
        self.data_sweetspot.setAccessibleName("Digital signal audio center")
        sweetspot_label = QLabel("Sweetspot")
        sweetspot_label.setBuddy(self.data_sweetspot)
        controls.addWidget(sweetspot_label)
        controls.addWidget(self.data_sweetspot)
        self.data_start = QPushButton("Start decoding")
        self.data_start.clicked.connect(self._toggle_data)
        controls.addWidget(self.data_start)
        controls.addStretch()
        layout.addLayout(controls)
        source_row = QHBoxLayout()
        self.data_source = QComboBox()
        self.data_source.addItems(["Receiver audio", "Audio file"])
        self.data_source.setAccessibleName("Decoder audio source")
        self.data_file = QLineEdit(readOnly=True, placeholderText="Choose an audio recording…")
        self.data_file.setAccessibleName("Audio recording path")
        self.data_choose_file = QPushButton("Choose…")
        self.data_choose_file.clicked.connect(self._choose_data_file)
        source_row.addWidget(self.data_source)
        source_row.addWidget(self.data_file, 1)
        source_row.addWidget(self.data_choose_file)
        layout.addLayout(source_row)
        squelch_row = QHBoxLayout()
        self.data_squelch = QCheckBox("Squelch", checked=True)
        self.data_threshold = QSpinBox(minimum=0, maximum=100, value=20)
        self.data_threshold.setKeyboardTracking(False)
        self.data_threshold.setAccessibleName("Decoder squelch threshold")
        squelch_row.addWidget(self.data_squelch)
        squelch_row.addWidget(self.data_threshold)
        squelch_row.addWidget(QLabel("Quality"))
        self.data_quality = QProgressBar(minimum=0, maximum=100, value=0)
        self.data_quality.setToolTip("Decoder quality, 0–100. Squelch suppresses text below the threshold.")
        squelch_row.addWidget(self.data_quality, 1)
        self.data_gate = QLabel("Closed")
        squelch_row.addWidget(self.data_gate)
        layout.addLayout(squelch_row)
        hint = QLabel("Tune the receiver on Listen, normally in USB. Sweetspot is the signal's "
                      "center in audio Hz. For decoding, turn off Auto Notch and Noise Reduction "
                      "on DSP.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.data_status = QLabel("Decoding stopped")
        self.data_status.setWordWrap(True)
        layout.addWidget(self.data_status)
        self.data_text = QPlainTextEdit(readOnly=True, maximumBlockCount=2000)
        self.data_text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.data_text.setAccessibleName("Decoded digital text")
        self.data_text.setPlaceholderText("Decoded text will appear here.")
        layout.addWidget(self.data_text, 1)
        clear = QPushButton("Clear text")
        clear.clicked.connect(self.data_text.clear)
        layout.addWidget(clear, alignment=Qt.AlignRight)
        self.data_mode.currentTextChanged.connect(self._data_mode_changed)
        self.data_source.currentIndexChanged.connect(self._data_source_changed)
        self.data_squelch.toggled.connect(self._data_squelch_changed)
        self.data_threshold.valueChanged.connect(self._data_squelch_changed)
        self._data_mode_changed()
        self._data_source_changed()
        return page

    def _data_mode_changed(self, *_):
        half = DATA_MODES[self.data_mode.currentText()] / 2
        self.data_sweetspot.setRange(half + 1, 3000 - half - 1)
        self.data_sweetspot.setEnabled(not self._data_active and self.data_mode.currentText() != "Auto (RSID)")

    def _data_source_changed(self, *_):
        file_input = self.data_source.currentIndex() == 1
        self.data_file.setEnabled(file_input and not self._data_active)
        self.data_choose_file.setEnabled(file_input and not self._data_active)

    def _choose_data_file(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Choose audio to decode", self.data_file.text(),
            "Audio recordings (*.wav *.m4a *.mp4 *.flac *.ogg *.mp3);;All files (*)")
        if filename:
            self.data_file.setText(filename)

    def _data_squelch_changed(self, *_):
        self.data_threshold.setEnabled(self.data_squelch.isChecked())
        self.data_gate.setText("Open" if not self.data_squelch.isChecked()
                              or self.data_quality.value() >= self.data_threshold.value() else "Closed")
        self.service.set_data_squelch(self.data_squelch.isChecked(), self.data_threshold.value())

    def _toggle_data(self):
        try:
            if self._data_active:
                self.service.stop_data()
            else:
                self.service.start_data(
                    self.data_mode.currentText(), self.data_sweetspot.value(),
                    squelch=self.data_squelch.isChecked(), threshold=self.data_threshold.value(),
                    source=self.data_file.text() if self.data_source.currentIndex() == 1 else None)
        except (ValueError, RuntimeError, OSError) as error:
            self.data_status.setText(str(error))

    def _append_data_text(self, text):
        cursor = self.data_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        for part in text.split("\b")[:-1]:
            cursor.insertText(part)
            cursor.deletePreviousChar()
        cursor.insertText(text.rsplit("\b", 1)[-1])
        # A transmission may contain no newline for hours.
        excess = self.data_text.document().characterCount() - 200000
        if excess > 0:
            cursor.setPosition(0)
            cursor.setPosition(excess, cursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        self.data_text.moveCursor(cursor.MoveOperation.End)

    def _refresh_log(self):
        batch = self._log_buffer.drain()
        if batch:
            self.log_view.appendPlainText(batch)

    def _build_flamp_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel("Files received through Data appear here. Valid blocks are retained across "
                      "repeats while the app is open, so later transmissions can fill missing blocks.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        folder_row = QHBoxLayout()
        self.flamp_directory = QLineEdit()
        self.flamp_directory.setAccessibleName("FLAMP receive folder")
        self.flamp_directory.editingFinished.connect(self._configure_flamp)
        choose = QPushButton("Choose folder…")
        choose.clicked.connect(self._choose_flamp_folder)
        folder_row.addWidget(self.flamp_directory, 1)
        folder_row.addWidget(choose)
        layout.addLayout(folder_row)
        self.flamp_auto_save = QCheckBox("Automatically save completed files", checked=True)
        self.flamp_auto_save.toggled.connect(self._configure_flamp)
        layout.addWidget(self.flamp_auto_save)
        self.flamp_table = QTableWidget(0, 5)
        self.flamp_table.setHorizontalHeaderLabels(["File", "Blocks", "Missing", "Status", "ID"])
        self.flamp_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.flamp_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.flamp_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.flamp_table.verticalHeader().hide()
        self.flamp_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.flamp_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.flamp_table.itemSelectionChanged.connect(self._flamp_selection_changed)
        layout.addWidget(self.flamp_table, 1)
        self.flamp_details = QLabel("No FLAMP transfers received")
        self.flamp_details.setTextFormat(Qt.PlainText)
        self.flamp_details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.flamp_details.setWordWrap(True)
        layout.addWidget(self.flamp_details)
        self.flamp_status = QLabel("")
        self.flamp_status.setTextFormat(Qt.PlainText)
        self.flamp_status.setWordWrap(True)
        layout.addWidget(self.flamp_status)
        row = QHBoxLayout()
        self.flamp_save = QPushButton("Save selected")
        self.flamp_save.setEnabled(False)
        self.flamp_save.clicked.connect(self._save_flamp)
        self.flamp_clear = QPushButton("Clear selected transfer")
        self.flamp_clear.setEnabled(False)
        self.flamp_clear.clicked.connect(self._clear_flamp)
        row.addWidget(self.flamp_save)
        row.addWidget(self.flamp_clear)
        row.addStretch()
        layout.addLayout(row)
        return page

    def _configure_flamp(self, *_):
        try:
            self.service.configure_flamp(self.flamp_directory.text(), self.flamp_auto_save.isChecked())
            return True
        except (ValueError, RuntimeError) as error:
            self.flamp_status.setText(str(error))
            return False

    def _choose_flamp_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "FLAMP receive folder", self.flamp_directory.text())
        if folder:
            self.flamp_directory.setText(folder)
            self._configure_flamp()

    def _selected_flamp(self):
        row = self.flamp_table.currentRow()
        return self._flamp_transfers[row] if 0 <= row < len(self._flamp_transfers) else None

    def _flamp_selection_changed(self):
        transfer = self._selected_flamp()
        self.flamp_save.setEnabled(bool(transfer and transfer["ready"]))
        self.flamp_clear.setEnabled(transfer is not None)
        if transfer:
            detail = "%s · %s" % (transfer["filename"], transfer["status"])
            if transfer["sender"]:
                detail += "\nFrom " + transfer["sender"]
            if transfer["missing"]:
                detail += "\nMissing blocks: " + transfer["missing"]
            if transfer["path"]:
                detail += "\n" + transfer["path"]
            self.flamp_details.setText(detail)
        else:
            self.flamp_details.setText("No FLAMP transfer selected")

    def _show_flamp_transfers(self, transfers):
        previous = self._selected_flamp()
        identifier = previous["id"] if previous else None
        self._flamp_transfers = transfers
        self.flamp_table.blockSignals(True)
        self.flamp_table.setRowCount(len(transfers))
        for row, transfer in enumerate(transfers):
            total = "?" if transfer["total"] is None else str(transfer["total"])
            values = [transfer["filename"], "%d / %s" % (transfer["received"], total),
                      transfer["missing"], transfer["status"], transfer["id"]]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.flamp_table.setItem(row, column, item)
            if transfer["id"] == identifier:
                self.flamp_table.selectRow(row)
        if transfers and self.flamp_table.currentRow() < 0:
            self.flamp_table.selectRow(0)
        self.flamp_table.blockSignals(False)
        self._flamp_selection_changed()

    def _save_flamp(self):
        transfer = self._selected_flamp()
        if transfer and self._configure_flamp():
            self.service.save_flamp(transfer["id"])

    def _clear_flamp(self):
        transfer = self._selected_flamp()
        if transfer:
            self.service.clear_flamp(transfer["id"])

    def _export_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export log", "kiwisdr-remote.log",
                                              "Log files (*.log);;Text files (*.txt);;All files (*)")
        if path:
            self._refresh_log()
            try:
                Path(path).write_text(self.log_view.toPlainText() + "\n", encoding="utf-8")
            except OSError as e:
                QMessageBox.warning(self, "Export log", str(e))

    def _build_audio_box(self):
        box = QGroupBox("Audio output device (virtual cable / speakers)")
        g = QGridLayout(box)
        self.audio_enable = QCheckBox("Play to")
        self.audio_enable.toggled.connect(self._audio_toggled)
        self.device_combo = QComboBox()
        self.device_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.currentIndexChanged.connect(self._device_changed)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_devices)
        self.volume = QSlider(Qt.Horizontal, minimum=0, maximum=200, value=100)
        self.volume.setToolTip("Output gain (100% = unity)")
        self.volume.sliderReleased.connect(self._volume_changed)
        self.volume_label = QLabel("100%")
        self.volume.valueChanged.connect(lambda v: self.volume_label.setText("%d%%" % v))
        self.audio_stats = QLabel("")
        self.audio_stats.setStyleSheet("color: gray")
        g.addWidget(self.audio_enable, 0, 0)
        g.addWidget(self.device_combo, 0, 1)
        g.addWidget(refresh, 0, 2)
        if sys.platform.startswith("linux") and shutil.which("pactl"):
            mk = QPushButton("Create 'KiwiSDR' virtual cable")
            mk.setToolTip("Creates sink 'KiwiSDR' (play to it here) and input source\n"
                          "'KiwiSDR-RX' (select it as Input in WSJT-X / JS8Call / fldigi).")
            mk.clicked.connect(self._create_pulse_sink)
            g.addWidget(mk, 3, 1, 1, 2)
        g.addWidget(QLabel("Gain"), 1, 0)
        g.addWidget(self.volume, 1, 1)
        g.addWidget(self.volume_label, 1, 2)
        g.addWidget(self.audio_stats, 2, 1, 1, 2)
        g.setColumnStretch(1, 1)
        return box

    def _build_record_box(self):
        box = QGroupBox("Record to file")
        g = QGridLayout(box)
        self.rec_format = QComboBox()
        self.rec_format.addItem("WAV (16-bit PCM, native rate)", ".wav")
        self.rec_format.addItem("M4A (AAC in MP4 container)", ".m4a")
        self.rec_format.addItem("MP4 (AAC, .mp4 extension)", ".mp4")
        if not HAVE_PYAV:
            for i in (1, 2):
                item = self.rec_format.model().item(i)
                item.setEnabled(False)
                item.setToolTip("Install PyAV:  pip install av")
        self.rec_dir = QLineEdit()
        self.rec_dir.setToolTip("Also the default folder for recordings started through the API")
        self.rec_dir.editingFinished.connect(self._rec_dir_changed)
        browse = QPushButton("Choose…")
        browse.clicked.connect(self._choose_dir)
        self.rec_btn = QPushButton("● Record")
        self.rec_btn.setMinimumWidth(100)
        self.rec_btn.setObjectName("recordAction")
        self.rec_btn.setToolTip("Start / stop recording (Ctrl+R; Command+R on macOS)")
        self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self._record_toggled)
        self.rec_label = QLabel("Ready to record")
        self.rec_label.setWordWrap(True)
        self.rec_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        g.addWidget(QLabel("Format"), 0, 0)
        g.addWidget(self.rec_format, 0, 1, 1, 2)
        g.addWidget(QLabel("Folder"), 1, 0)
        g.addWidget(self.rec_dir, 1, 1)
        folder_actions = QHBoxLayout()
        folder_actions.addWidget(browse)
        reveal = QPushButton("Open")
        reveal.setToolTip("Open the recording folder")
        reveal.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(
            os.path.abspath(os.path.expanduser(self.rec_dir.text())))))
        folder_actions.addWidget(reveal)
        g.addLayout(folder_actions, 1, 2)
        g.addWidget(self.rec_btn, 2, 0)
        g.addWidget(self.rec_label, 2, 1, 1, 2)
        g.setColumnStretch(1, 1)
        return box

    def _build_rigctl_box(self):
        box = QGroupBox("Hamlib rigctld (CAT control for WSJT-X, fldigi, JS8Call …)")
        g = QGridLayout(box)
        self.rig_enable = QCheckBox("Enable")
        self.rig_enable.toggled.connect(self._toggle_rigctld)
        self.rig_addr = QComboBox(editable=True)
        self.rig_addr.addItems(["127.0.0.1", "0.0.0.0", "::1"])
        self.rig_addr.setToolTip("127.0.0.1 = this computer only; 0.0.0.0 = allow LAN clients")
        self.rig_port = QSpinBox(minimum=1, maximum=65535, value=4532)
        self.rig_status = QLabel("Off")
        hint = QLabel("In the client choose rig 'Hamlib NET rigctl' (model 2), "
                      "address 127.0.0.1:<port>.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        g.addWidget(self.rig_enable, 0, 0)
        g.addWidget(QLabel("Address"), 0, 1)
        g.addWidget(self.rig_addr, 0, 2)
        g.addWidget(QLabel("Port"), 0, 3)
        g.addWidget(self.rig_port, 0, 4)
        g.addWidget(self.rig_status, 1, 0, 1, 5)
        g.addWidget(hint, 2, 0, 1, 5)
        return box

    def _build_api_box(self):
        box = QGroupBox("Remote control API and session limits")
        g = QGridLayout(box)
        self.api_enable = QCheckBox("API on 127.0.0.1, port")
        self.api_enable.setToolTip("HTTP/JSON API for schedulers and scripts; see docs/API.md")
        self.api_enable.toggled.connect(self._toggle_api)
        self.api_port = QSpinBox(minimum=1, maximum=65535, value=API_DEFAULT_PORT)
        self.api_token = QLineEdit(echoMode=QLineEdit.Password, placeholderText="token (optional)")
        self.api_status = QLabel("Off")
        g.addWidget(self.api_enable, 0, 0, 1, 2)
        g.addWidget(self.api_port, 0, 2)
        g.addWidget(self.api_token, 0, 3, 1, 3)
        g.addWidget(self.api_status, 1, 0, 1, 6)

        def minutes(tip):
            w = QSpinBox(minimum=0, maximum=24 * 60, singleStep=15, suffix=" min")
            w.setSpecialValueText("off")
            w.setToolTip(tip)
            w.valueChanged.connect(self._policy_changed)
            return w
        self.lim_pw = minutes("Session limit when a password is used (0 = off)")
        self.lim_pub = minutes("Session limit without a password (0 = off)")
        self.reset_min = minutes("Disconnect and reconnect after this much connected time (0 = off)")
        self.reset_pause = QSpinBox(minimum=1, maximum=300, suffix=" s")
        self.reset_pause.setToolTip("How long a periodic reset stays disconnected")
        self.reset_pause.valueChanged.connect(self._policy_changed)
        self.exempt_local = QCheckBox("No limits or resets for Kiwis on this computer / LAN")
        self.exempt_local.toggled.connect(self._policy_changed)
        g.addWidget(QLabel("Limit with password"), 2, 0)
        g.addWidget(self.lim_pw, 2, 1)
        g.addWidget(QLabel("without"), 2, 2)
        g.addWidget(self.lim_pub, 2, 3)
        g.addWidget(QLabel("Reset every"), 3, 0)
        g.addWidget(self.reset_min, 3, 1)
        g.addWidget(QLabel("pause"), 3, 2)
        g.addWidget(self.reset_pause, 3, 3)
        g.addWidget(self.exempt_local, 4, 0, 1, 6)
        return box

    # ================================================================== settings
    POLICY_VERSION = 2   # 2: passworded sessions have no limit by default (reconnect and keep going)

    def _load_policy(self):
        s, d = self.settings, SessionPolicy()
        old = s.value("policy_version", 1, type=int) < self.POLICY_VERSION
        return SessionPolicy(
            d.limit_with_password_min if old else s.value("limit_password_min", d.limit_with_password_min, type=float),
            s.value("limit_public_min", d.limit_without_password_min, type=float),
            s.value("reset_interval_min", d.reset_interval_min, type=float),
            s.value("reset_pause_s", d.reset_pause_s, type=float),
            s.value("exempt_local", d.exempt_local, type=bool))

    def _load_settings(self):
        s = self.settings
        mode = s.value("data_mode", "MFSK32")
        self.data_mode.setCurrentText(mode if mode in DATA_MODES else "MFSK32")
        self.data_sweetspot.setValue(s.value("data_sweetspot_hz", 1500, type=float))
        self.data_squelch.setChecked(s.value("data_squelch", True, type=bool))
        self.data_threshold.setValue(s.value("data_threshold", 20, type=int))
        default_flamp = str(Path(QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation))
                            / "KiwiSDR" / "Received")
        self.flamp_directory.setText(s.value("flamp_directory", default_flamp))
        self.flamp_auto_save.setChecked(s.value("flamp_auto_save", True, type=bool))
        self._configure_flamp()
        hist = s.value("server_history", [], type=list) or []
        self.server_combo.addItems(hist)
        self.server_combo.setCurrentText(s.value("server", hist[0] if hist else ""))
        self.user_name.setText(s.value("user", "kiwiremote"))
        self.compression.setChecked(s.value("compression", True, type=bool))
        self.tls.setChecked(s.value("tls", False, type=bool))
        mode = s.value("mode", "USB")
        mode = mode if mode in MODES else "USB"
        pb = s.value("passband", DEFAULT_PASSBAND[mode], type=int)
        self.service.tune(mode=mode, passband=pb)
        self.service.tune(freq_hz=s.value("freq_hz", 7_074_000, type=int))
        self._show_tune(self.radio.tune)
        enabled, gate_us, threshold = DEFAULT_NOISE_BLANKER
        self.nb_enabled.setChecked(s.value("nb_enabled", enabled, type=bool))
        self.nb_gate.setValue(s.value("nb_gate_us", gate_us, type=int))
        self.nb_threshold.setValue(s.value("nb_threshold", threshold, type=int))
        self._apply_noise_blanker()
        self.nb_enabled.toggled.connect(self._apply_noise_blanker)
        self.nb_gate.valueChanged.connect(self._apply_noise_blanker)
        self.nb_threshold.valueChanged.connect(self._apply_noise_blanker)
        enabled, center, width = DEFAULT_AUDIO_BANDPASS
        self.bandpass_enabled.setChecked(s.value("audio_bandpass_enabled", enabled, type=bool))
        self.bandpass_center.setValue(s.value("audio_bandpass_center", center, type=int))
        self.bandpass_width.setValue(s.value("audio_bandpass_width", width, type=int))
        self._apply_audio_bandpass()
        self.bandpass_enabled.toggled.connect(self._apply_audio_bandpass)
        self.bandpass_center.valueChanged.connect(self._apply_audio_bandpass)
        self.bandpass_width.valueChanged.connect(self._apply_audio_bandpass)
        enabled, frequency, width = DEFAULT_AUDIO_NOTCH
        self.notch_enabled.setChecked(s.value("audio_notch_enabled", enabled, type=bool))
        self.notch_frequency.setValue(s.value("audio_notch_frequency", frequency, type=int))
        self.notch_width.setValue(s.value("audio_notch_width", width, type=int))
        self._apply_manual_notch()
        self.notch_enabled.toggled.connect(self._apply_manual_notch)
        self.notch_frequency.valueChanged.connect(self._apply_manual_notch)
        self.notch_width.valueChanged.connect(self._apply_manual_notch)
        self.auto_notch.setChecked(s.value("auto_notch", False, type=bool))
        self.noise_reduction.setChecked(s.value("noise_reduction", False, type=bool))
        self._apply_receiver_filters()
        self.auto_notch.toggled.connect(self._apply_receiver_filters)
        self.noise_reduction.toggled.connect(self._apply_receiver_filters)
        idx = self.step.findData(s.value("step", 1000, type=int))
        self.step.setCurrentIndex(idx if idx >= 0 else 3)
        self._step_changed()
        self._refresh_devices(select=s.value("audio_device", ""))
        self.volume.setValue(s.value("volume", 100, type=int))
        i = self.rec_format.findData(s.value("rec_format", ".wav"))
        self.rec_format.setCurrentIndex(i if i >= 0 and self.rec_format.model().item(i).isEnabled() else 0)
        self.rec_dir.setText(s.value("rec_dir", default_record_dir()))
        self._rec_dir_changed()
        pol = self.service.policy
        for w, v in ((self.lim_pw, pol.limit_with_password_min), (self.lim_pub, pol.limit_without_password_min),
                     (self.reset_min, pol.reset_interval_min), (self.reset_pause, pol.reset_pause_s)):
            w.blockSignals(True)
            w.setValue(int(v))
            w.blockSignals(False)
        self.exempt_local.blockSignals(True)
        self.exempt_local.setChecked(pol.exempt_local)
        self.exempt_local.blockSignals(False)
        self.api_port.setValue(s.value("api_port", API_DEFAULT_PORT, type=int))
        self.api_token.setText(s.value("api_token", ""))
        self.api_enable.blockSignals(True)
        self.api_enable.setChecked(s.value("api_enable", False, type=bool))
        self.api_enable.blockSignals(False)
        self.rig_addr.setCurrentText(s.value("rig_addr", "127.0.0.1"))
        self.rig_port.setValue(s.value("rig_port", 4532, type=int))
        self.rig_enable.blockSignals(True)
        self.rig_enable.setChecked(s.value("rig_enable", True, type=bool))
        self.rig_enable.blockSignals(False)
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        if s.value("layout_version", 0, type=int) < 2:
            self.resize(680, 600)
        if s.value("audio_enable", False, type=bool) and self.device_combo.currentData():
            self.audio_enable.setChecked(True)

    def _save_settings(self):
        s = self.settings
        s.setValue("data_mode", self.data_mode.currentText())
        s.setValue("data_sweetspot_hz", self.data_sweetspot.value())
        s.setValue("data_squelch", self.data_squelch.isChecked())
        s.setValue("data_threshold", self.data_threshold.value())
        s.setValue("flamp_directory", self.flamp_directory.text())
        s.setValue("flamp_auto_save", self.flamp_auto_save.isChecked())
        hist = [self.server_combo.itemText(i) for i in range(self.server_combo.count())]
        s.setValue("server_history", hist)
        s.setValue("server", self.server_combo.currentText())
        s.setValue("user", self.user_name.text())
        s.setValue("compression", self.compression.isChecked())
        s.setValue("tls", self.tls.isChecked())
        st = self.radio.tune
        s.setValue("freq_hz", st.freq_hz)
        s.setValue("mode", st.mode)
        s.setValue("passband", st.passband)
        s.setValue("nb_enabled", self.nb_enabled.isChecked())
        s.setValue("nb_gate_us", self.nb_gate.value())
        s.setValue("nb_threshold", self.nb_threshold.value())
        s.setValue("audio_bandpass_enabled", self.bandpass_enabled.isChecked())
        s.setValue("audio_bandpass_center", self.bandpass_center.value())
        s.setValue("audio_bandpass_width", self.bandpass_width.value())
        s.setValue("audio_notch_enabled", self.notch_enabled.isChecked())
        s.setValue("audio_notch_frequency", self.notch_frequency.value())
        s.setValue("audio_notch_width", self.notch_width.value())
        s.setValue("auto_notch", self.auto_notch.isChecked())
        s.setValue("noise_reduction", self.noise_reduction.isChecked())
        s.setValue("step", self.step.currentData())
        s.setValue("audio_device", self.device_combo.currentData() or "")
        s.setValue("audio_enable", self.audio_enable.isChecked())
        s.setValue("volume", self.volume.value())
        s.setValue("rec_format", self.rec_format.currentData())
        s.setValue("rec_dir", self.rec_dir.text())
        s.setValue("rig_enable", self.rig_enable.isChecked())
        s.setValue("rig_addr", self.rig_addr.currentText())
        s.setValue("rig_port", self.rig_port.value())
        pol = self.service.policy
        s.setValue("limit_password_min", pol.limit_with_password_min)
        s.setValue("policy_version", self.POLICY_VERSION)
        s.setValue("limit_public_min", pol.limit_without_password_min)
        s.setValue("reset_interval_min", pol.reset_interval_min)
        s.setValue("reset_pause_s", pol.reset_pause_s)
        s.setValue("exempt_local", pol.exempt_local)
        s.setValue("api_enable", self.api_enable.isChecked())
        s.setValue("api_port", self.api_port.value())
        s.setValue("api_token", self.api_token.text())
        s.setValue("geometry", self.saveGeometry())
        s.setValue("layout_version", 2)

    # ================================================================== server
    def _connect_clicked(self, *_):
        if self.radio.running or self.connect_btn.text() == "Disconnect":
            self.service.disconnect(reason="ui")
            return
        text = self.server_combo.currentText()
        try:
            spec = ServerSpec.parse(text, self.password.text(), self.user_name.text().strip(),
                                    tls=self.tls.isChecked())
        except (ValueError, ApiError) as e:
            QMessageBox.warning(self, "Server", "Invalid server address: %s" % e)
            return
        self._remember_server(spec.label())
        try:
            self.service.connect(spec, compression=self.compression.isChecked())
        except (ApiError, RuntimeError) as error:
            self.statusBar().showMessage(str(error), 8000)

    def _server_picked(self, text):
        # History entries carry https:// when they were TLS (ServerSpec.label)
        self.tls.setChecked(text.strip().lower().startswith(("https://", "wss://")))

    def _remember_server(self, label):
        if self.server_combo.count() and self.server_combo.itemText(0) == label:
            self.server_combo.setCurrentIndex(0)
            return
        i = self.server_combo.findText(label)
        if i >= 0:
            self.server_combo.removeItem(i)
        self.server_combo.insertItem(0, label)
        while self.server_combo.count() > MAX_HISTORY:
            self.server_combo.removeItem(self.server_combo.count() - 1)
        self.server_combo.setCurrentIndex(0)

    # ================================================================== tuning
    def _show_tune(self, st):
        for w in (self.freq, self.passband):
            w.blockSignals(True)
        self.freq.setValue(st.freq_hz / 1000.0)
        self.passband.setValue(st.passband)
        self.mode_buttons[st.mode].setChecked(True)
        for w in (self.freq, self.passband):
            w.blockSignals(False)

    def _step_changed(self, *_):
        step_hz = self.step.currentData() or 1000
        self.freq.setSingleStep(step_hz / 1000.0)

    @Slot(float)
    def _freq_edited(self, khz):
        try:
            self.service.tune(freq_hz=khz * 1000.0)
        except (ValueError, ApiError) as e:
            self.statusBar().showMessage(str(e), 5000)
            self._show_tune(self.radio.tune)

    def _mode_clicked(self, button):
        mode = button.text()
        self.service.tune(mode=mode, passband=DEFAULT_PASSBAND[mode])
        self._show_tune(self.radio.tune)

    @Slot(int)
    def _passband_edited(self, hz):
        self.service.tune(mode=self.radio.tune.mode, passband=hz)

    # ================================================================== audio device
    def _refresh_devices(self, select=None):
        current = select if isinstance(select, str) and select else self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        devices = list_output_devices()
        for key, label in devices:
            self.device_combo.addItem(label, key)
        if not devices:
            self.device_combo.addItem("(no output devices found)", None)
        idx = self.device_combo.findData(current) if current else -1
        if idx < 0:
            guess = guess_virtual_cable(devices)
            idx = self.device_combo.findData(guess) if guess else 0
        self.device_combo.setCurrentIndex(max(idx, 0))
        self.device_combo.blockSignals(False)

    @Slot(bool)
    def _audio_toggled(self, on):
        key = self.device_combo.currentData() if on else None
        if on and not key:
            self.audio_enable.setChecked(False)
            return
        try:
            self.radio.set_audio_device(key, self.volume.value() / 100.0)
        except Exception as e:
            QMessageBox.warning(self, "Audio device", str(e))
            self.audio_enable.blockSignals(True)
            self.audio_enable.setChecked(False)
            self.audio_enable.blockSignals(False)
            self.audio_stats.setText("")

    def _create_pulse_sink(self, *_):
        try:
            sink, source = create_virtual_cable("KiwiSDR")
        except Exception as e:
            QMessageBox.warning(self, "Virtual cable", str(e))
            return
        self._refresh_devices(select=PULSE_PREFIX + sink)
        self._device_changed()
        QMessageBox.information(
            self, "Virtual cable",
            "Created sink '%s' and input source '%s-RX'.\n\n"
            "In WSJT-X / JS8Call / fldigi select Input = %s-RX, then restart that "
            "program (they only list audio devices at startup).\n\n"
            "The cable lasts until logout; see the README to make it permanent."
            % (sink, sink, sink))

    def _device_changed(self, *_):
        if self.audio_enable.isChecked():
            self._audio_toggled(True)

    def _volume_changed(self, *_):
        sink = self.radio._sinks.get("card")
        if sink is not None:
            sink.gain = self.volume.value() / 100.0

    # ================================================================== recording
    def _choose_dir(self, *_):
        d = QFileDialog.getExistingDirectory(self, "Recording folder", self.rec_dir.text())
        if d:
            self.rec_dir.setText(d)
            self._rec_dir_changed()

    def _rec_dir_changed(self, *_):
        self.service.record_dir = self.rec_dir.text().strip() or default_record_dir()

    @Slot(bool)
    def _record_toggled(self, on):
        # The button only asks; the 'recording' event updates it (also for API starts/stops).
        self._set_rec_button(not on)
        if not on:
            try:
                self.service.stop_recording(reason="ui")
            except (ApiError, OSError, RuntimeError) as error:
                self.statusBar().showMessage(str(error), 8000)
            return
        st = self.radio.tune
        name = default_recording_name(st.freq_hz, st.mode, self.rec_format.currentData())
        try:
            self._rec_dir_changed()
            self.service.start_recording(path=os.path.join(self.service.record_dir, name),
                                         formats=(".wav", ".m4a", ".mp4"))
        except (ApiError, OSError, ValueError, RuntimeError) as e:
            QMessageBox.warning(self, "Record", str(e))

    def _set_rec_button(self, active):
        self.rec_btn.blockSignals(True)
        self.rec_btn.setChecked(active)
        self.rec_btn.setText("■ Stop" if active else "● Record")
        self.rec_btn.blockSignals(False)
        self.rec_format.setEnabled(not active)
        self.rec_dir.setEnabled(not active)

    # ================================================================== API / policy
    def _policy_changed(self, *_):
        p = self.service.policy
        p.limit_with_password_min = float(self.lim_pw.value())
        p.limit_without_password_min = float(self.lim_pub.value())
        p.reset_interval_min = float(self.reset_min.value())
        p.reset_pause_s = float(self.reset_pause.value())
        p.exempt_local = self.exempt_local.isChecked()
        # takes effect on the next connect

    @Slot(bool)
    def _toggle_api(self, on):
        if self.api is not None:
            self.api.stop()
            self.api = None
        self.api_port.setEnabled(not on)
        self.api_token.setEnabled(not on)
        if not on:
            self.api_status.setText("Off")
            return
        try:
            self.api = ApiServer(self.service, "127.0.0.1", self.api_port.value(),
                                 self.api_token.text().strip() or None)
            self.api.start()
        except (OSError, ValueError) as e:
            self.api = None
            self.api_status.setText("Could not start: %s" % e)
            self.api_enable.blockSignals(True)
            self.api_enable.setChecked(False)
            self.api_enable.blockSignals(False)
            self.api_port.setEnabled(True)
            self.api_token.setEnabled(True)
            return
        self.api_status.setText("Listening on %s%s" % (self.api.url, "  (token required)" if self.api.token else ""))

    # ================================================================== rigctld
    @Slot(bool)
    def _toggle_rigctld(self, on):
        if self.rigctld is not None:
            self.rigctld.stop()
            self.rigctld = None
        self.rig_addr.setEnabled(not on)
        self.rig_port.setEnabled(not on)
        if not on:
            self.rig_status.setText("Off")
            return
        try:
            self.rigctld = RigctldServer(self.radio, self.rig_addr.currentText().strip(),
                                         self.rig_port.value(), on_activity=self._rig_activity, service=self.service)
            self.rigctld.start()
        except OSError as e:
            self.rigctld = None
            self.rig_status.setText("Could not listen: %s" % e)
            self.rig_enable.blockSignals(True)
            self.rig_enable.setChecked(False)
            self.rig_enable.blockSignals(False)
            self.rig_addr.setEnabled(True)
            self.rig_port.setEnabled(True)
            return
        self._rig_activity("clients", (0, None, False))

    def _rig_activity(self, kind, payload):
        # called from rigctld threads -> hop to GUI thread
        self.bridge.event.emit("rig_" + kind, payload)

    # ================================================================== events (GUI thread)
    @Slot(str, object)
    def _on_event(self, kind, payload):
        if kind == "flamp_transfers":
            self._show_flamp_transfers(payload)
        elif kind == "flamp_status":
            self.flamp_status.setText(payload)
        elif kind in ("data_state", "data_text", "data_quality"):
            session = payload["session"]
            if session < self._data_session:
                return
            self._data_session = session
            if kind == "data_text":
                self._append_data_text(payload["text"])
            elif kind == "data_quality":
                self.data_quality.setValue(round(payload["value"]))
                self.data_gate.setText("Open" if not self.data_squelch.isChecked()
                                      or self.data_quality.value() >= self.data_threshold.value() else "Closed")
            else:
                self._data_active = payload["active"]
                if (self.data_mode.currentText() == "Auto (RSID)"
                        and payload.get("state") == "receiving" and "sweetspot_hz" in payload):
                    self.data_sweetspot.setValue(payload["sweetspot_hz"])
                self.data_start.setText("Stop decoding" if self._data_active else "Start decoding")
                self.data_mode.setEnabled(not self._data_active)
                self._data_mode_changed()
                self.data_source.setEnabled(not self._data_active)
                self._data_source_changed()
                if not self._data_active:
                    self.data_quality.setValue(0)
                    self.data_gate.setText("Closed" if self.data_squelch.isChecked() else "Open")
                self.data_status.setText(payload["message"])
        elif kind == "tune":
            st, source = payload
            self._show_tune(st)
            if source == "rigctl":
                self.statusBar().showMessage("rigctl: %.3f kHz %s %d Hz"
                                             % (st.freq_hz / 1e3, st.mode, st.passband), 3000)
        elif kind == "status":
            text, level = payload
            self.statusBar().showMessage(text)
            self.conn_label.setText(text)
            self.conn_label.setStyleSheet("color: #c0392b" if level == "error" else "")
        elif kind == "recording":
            self._set_rec_button(payload["active"])
            if payload["active"]:
                self._rec_started = time.monotonic()
                self.rec_label.setText(os.path.basename(payload["path"]))
            else:
                self._rec_started = None
                self.rec_label.setText("Saved %s (%.0f s)" % (os.path.basename(payload["path"]),
                                                              payload["seconds"]))
        elif kind == "session":
            if payload and payload.get("ended") is None:
                self._remember_server(payload["server"])
                self.tls.setChecked(bool(payload.get("tls")))
                self.connect_btn.setText("Disconnect")
            self._show_session(payload)
        elif kind == "state":
            state = payload[0]
            self.state_badge.setText(state.capitalize())
            colors = {"connected": "#166534", "failed": "#b42332", "busy": "#92400e",
                      "connecting": "#1d4ed8", "reconnecting": "#92400e"}
            color = colors.get(state, "#475569")
            self.state_badge.setStyleSheet("background: %s; color: white; border-radius: 10px; padding: 5px 12px" % color)
            info = self.service.session_info() or {}
            idle = state in ("idle", "failed") and "reset_in_progress_s" not in info
            self.connect_btn.setText("Connect" if idle else "Disconnect")
        elif kind == "connected":
            if not payload:
                self.smeter.setValue(-127)
                self.smeter.setFormat("—")
        elif kind == "smeter":
            dbm = float(payload)
            self.smeter.setValue(int(max(-127, min(-13, dbm))))
            self.smeter.setFormat("%s   %.0f dBm" % (s_units(dbm), dbm))
        elif kind == "limits":
            lo, hi = payload
            self.freq.blockSignals(True)
            self.freq.setRange(lo / 1000.0, hi / 1000.0)
            self.freq.blockSignals(False)
        elif kind == "sink_failed":
            if payload == "card":
                self.audio_enable.blockSignals(True)
                self.audio_enable.setChecked(False)
                self.audio_enable.blockSignals(False)
            elif payload == "file":
                self.rec_btn.blockSignals(True)
                self.rec_btn.setChecked(False)
                self.rec_btn.setText("● Record")
                self.rec_btn.blockSignals(False)
        elif kind == "rig_clients":
            n = payload[0]
            if self.rigctld is not None:
                self.rig_status.setText("Listening on %s:%d — %d client%s"
                                        % (self.rigctld.host, self.rigctld.port, n, "" if n == 1 else "s"))
        elif kind == "rig_command":
            pass

    def _show_session(self, info):
        if not info or info.get("ended"):
            self.session_label.setText("Session ended: %s" % info["ended"] if info and info.get("ended") else "")
            return
        parts = []
        if info.get("local"):
            parts.append("local Kiwi")
        if "remaining_s" in info:
            parts.append("limit in %s" % _hms(info["remaining_s"]))
        elif info.get("local") is not None and not info.get("limit_s"):
            parts.append("no session limit")
        if "reset_in_progress_s" in info:
            parts.append("periodic reset: reconnecting in %d s" % info["reset_in_progress_s"])
        elif "next_reset_s" in info:
            parts.append("reset in %s" % _hms(info["next_reset_s"]))
        if info.get("resets"):
            parts.append("%d reset%s" % (info["resets"], "" if info["resets"] == 1 else "s"))
        self.session_label.setText("   ·   ".join(parts))

    def _tick(self, *_):
        self._reload_quick_tune()
        self._refresh_log()
        self._show_session(self.service.session_info())
        secs = self.radio.recording_seconds()
        if secs is not None and self._rec_started is not None:
            wall = time.monotonic() - self._rec_started
            self.rec_label.setText("Recording  %02d:%02d:%02d" % (wall // 3600, wall % 3600 // 60, wall % 60)
                                   + ("" if self.radio.connected else "  (waiting for audio)"))
        stats = self.radio.audio_stats()
        if stats:
            self.audio_stats.setText("buffer %.0f ms   clock trim %+.0f ppm   underruns %d"
                                     % (stats["buffer_ms"], stats["trim_ppm"], stats["underruns"]))
        elif not self.audio_enable.isChecked():
            self.audio_stats.setText("")

    def closeEvent(self, ev):
        self._save_settings()
        if self.api is not None:
            self.api.stop()
        if self.rigctld is not None:
            self.rigctld.stop()
        self.service.shutdown()
        logging.getLogger().removeHandler(self._log_buffer)
        self._log_buffer.close()
        super().closeEvent(ev)


def _hms(sec):
    sec = int(sec)
    return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def run(argv):
    app = QApplication(argv)
    app.setApplicationName("KiwiSDR Remote")
    w = MainWindow()
    w.show()
    return app.exec()
