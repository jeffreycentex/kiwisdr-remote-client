# SPDX-License-Identifier: GPL-3.0-or-later
"""PySide6 main window. All actions go through KiwiService, so changes made by
the HTTP API or rigctld show up here (via events) exactly like local ones."""

import logging
import os
import shutil
import sys
import time

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QFont, QKeySequence, QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QRadioButton, QSlider, QSpinBox,
    QScrollArea, QTabWidget, QStatusBar, QVBoxLayout, QWidget)

from . import __version__
from .api import DEFAULT_PORT as API_DEFAULT_PORT, ApiServer
from .rigctld import RigctldServer
from .service import ApiError, KiwiService, SessionPolicy, default_record_dir
from .sinks import (HAVE_PYAV, HAVE_SOUNDDEVICE, PULSE_PREFIX, create_virtual_cable,
                    default_recording_name, guess_virtual_cable, list_output_devices,
                    sounddevice_error)
from .state import DEFAULT_PASSBAND, MODES, ServerSpec

log = logging.getLogger(__name__)

PUBLIC_LIST_URL = "http://rx.kiwisdr.com/"
STEPS_HZ = [10, 100, 500, 1000, 5000, 9000, 10000, 100000]
MAX_HISTORY = 15


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
            ("Listen & record", (self._build_server_box, self._build_tune_box,
                                 self._build_record_box)),
            ("Audio", (self._build_audio_box,)),
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

        self._load_settings()
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
        if kind == "tune":
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
