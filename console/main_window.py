"""The console main window — RadiQ-style layout over live gateway data."""

from __future__ import annotations

import os
import sys

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPixmap, QShortcut
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QPushButton, QSlider, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from datetime import datetime

from .cue_overlay import CueOverlay
from .data import GatewayData, load_console_settings

# Track-panel colours, echoing the legacy RadiQ dashboard.
COL_PREV, COL_CURRENT, COL_NEXT, COL_TIME = "#f6b99a", "#a9e39a", "#b9a8ef", "#77e6e6"
# OraMedia brand accent (from the brand sheet: orange #F08428).
COL_TAGLINE = "#F08428"

# Semantic value colours (readable on the dark panels).
OK, WARN, BAD, NEUTRAL = "#6fe08a", "#e6b26a", "#ef6b6b", "#cdd6e6"

_RESIZE = QHeaderView.ResizeMode

DARK_QSS = """
* { font-size: 10pt; }
QMainWindow, QWidget { background: #131a2b; color: #e6e9f0; }
QGroupBox { border: 1px solid #2a3550; border-radius: 6px; margin-top: 16px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #9fb2d8; }
QTableWidget {
    background: #0f1524; alternate-background-color: #141d31;
    gridline-color: #223; border: none; outline: 0;
}
QTableWidget::item { padding: 3px 6px; }
QTableWidget::item:selected { background: #2c4a7a; color: #ffffff; }
QHeaderView::section {
    background: #1b2540; color: #9fb2d8; border: 0; border-bottom: 1px solid #2a3550;
    padding: 5px 6px; font-weight: 600;
}
QTableCornerButton::section { background: #1b2540; border: 0; }
QScrollBar:vertical { background: #0f1524; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #2f3d5c; border-radius: 6px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QTabWidget::pane { border: 1px solid #2a3550; border-radius: 4px; }
QTabBar::tab { background: #172038; color: #cdd6e6; padding: 6px 16px; border: 1px solid #2a3550; }
QTabBar::tab:selected { background: #23314f; color: #ffffff; }
QMenuBar { background: #0f1524; color: #dbe2f0; }
QMenuBar::item { padding: 6px 12px; }
QMenuBar::item:selected { background: #23314f; }
QMenu { background: #172038; color: #dbe2f0; border: 1px solid #2a3550; }
QMenu::item:selected { background: #23314f; }
QStatusBar { background: #0f1524; border-top: 1px solid #2a3550; }
QToolTip { background: #0b1120; color: #e6e9f0; border: 1px solid #3a4a70; padding: 4px; }
QLabel#track { border: 1px solid #6b7285; border-radius: 6px; padding: 6px; color: #10131a; font-weight: 700; }
QLabel#caption { color: #9fb2d8; font-weight: 600; }
"""


def _res(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(base, "resources", name), os.path.join(base, "console", "resources", name)):
        if os.path.isfile(cand):
            return cand
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", name)


def _item(text: str, tip: str | None = None, color: str | None = None) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setToolTip(tip if tip is not None else text)
    if color:
        it.setForeground(QColor(color))
    return it


class _Signals(QObject):
    done = pyqtSignal(dict)


class SnapshotWorker(QRunnable):
    """Fetch a full snapshot off the UI thread so RadioBOSS/DB I/O never freezes
    the window (and only ONE runs at a time — no thread churn)."""
    def __init__(self, data: GatewayData) -> None:
        super().__init__()
        self.data = data
        self.signals = _Signals()

    def run(self) -> None:
        try:
            snap = self.data.snapshot()
        except Exception as exc:  # never let a bad refresh crash the UI thread
            snap = {"error": str(exc)}
        self.signals.done.emit(snap)


class GlanceWorker(QRunnable):
    """Fetch the slower ERP-backed dashboard data (today's schedule + the
    approved-content manifest) off the UI thread."""
    def __init__(self, data: GatewayData) -> None:
        super().__init__()
        self.data = data
        self.signals = _Signals()

    def run(self) -> None:
        try:
            g = self.data.fetch_glance()
        except Exception as exc:
            g = {"error": str(exc)}
        self.signals.done.emit(g)


class MainWindow(QMainWindow):
    HEALTH_ROWS = ("Service", "RadioBOSS", "Playback", "Uplink", "Queue pending", "Acks buffered")

    def __init__(self) -> None:
        super().__init__()
        self.settings, self.config_warning = load_console_settings()
        self.data = GatewayData(self.settings)
        self.pool = QThreadPool.globalInstance()
        self._refreshing = False
        self._glancing = False
        # Must-air presenter-cue overlay (Phase B).
        self._cue_overlay: CueOverlay | None = None
        self._dismissed_cues: set[str] = set()

        # Audio monitoring — "Listen" to the public broadcast stream.
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(0.8)
        self.player.errorOccurred.connect(self._on_media_error)

        self.setWindowTitle(f"MediaCore Studio Gateway — Console ({self._version()})")
        self.setWindowIcon(QIcon(_res("app.ico")))
        self.resize(1200, 720)
        self.setMinimumSize(1000, 620)
        self.setStyleSheet(DARK_QSS)

        self._build_menu()
        self._build_body()
        self._build_statusbar()

        # Ctrl+C copies the focused table's selection to the clipboard.
        QShortcut(QKeySequence.StandardKey.Copy, self, activated=self._copy_selection)

        if self.config_warning:
            self.statusBar().showMessage(self.config_warning, 10000)

        self._clock = QTimer(self); self._clock.timeout.connect(self._tick_clock)
        self._clock.setInterval(1000); self._clock.start()
        self._refresh_timer = QTimer(self); self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.setInterval(3000); self._refresh_timer.start()
        # Glanceable dashboards (ERP schedule + manifest) refresh on a slow cadence.
        self._glance_timer = QTimer(self); self._glance_timer.timeout.connect(self.refresh_glance)
        self._glance_timer.setInterval(60000); self._glance_timer.start()
        self._tick_clock()
        self.refresh()
        self.refresh_glance()

    # ── layout ──────────────────────────────────────────────────────────
    def _build_menu(self) -> None:
        mb = self.menuBar()
        m_prog = mb.addMenu("&Programs")
        m_prog.addAction(self._act("Running Order", self.refresh))
        m_ads = mb.addMenu("&Ads")
        m_ads.addAction(self._act("Open Media Folder", lambda: self._open(self.settings.media_folder)))
        m_ev = mb.addMenu("&Events")
        m_ev.addAction(self._act("Operations Log", lambda: self.tabs.setCurrentIndex(0)))
        m_tools = mb.addMenu("&Tools")
        m_tools.addAction(self._act("Refresh Now\tF5", self.refresh))
        m_tools.addAction(self._act("Open Log File", lambda: self._open(self.data.log_path())))
        m_tools.addAction(self._act("Open Data Folder", lambda: self._open(os.path.dirname(self.settings.db_path) or ".")))
        m_tools.addAction(self._act("RadioBOSS Web UI", lambda: self._open_url(self.settings.radioboss_base_url)))
        m_tools.addSeparator()
        m_tools.addAction(self._act("Collect Diagnostics…", self._collect_diagnostics))
        m_set = mb.addMenu("&Settings")
        m_set.addAction(self._act("View Configuration…", self._show_config))
        m_help = mb.addMenu("&Help")
        m_help.addAction(self._act("About", self._about))

        # OraMedia wordmark, top-right of the menu bar.
        brand = QLabel()
        pm = QPixmap(_res("wordmark_dark.png"))
        if not pm.isNull():
            brand.setPixmap(pm.scaledToHeight(22, Qt.TransformationMode.SmoothTransformation))
            brand.setContentsMargins(0, 0, 12, 0)
            mb.setCornerWidget(brand, Qt.Corner.TopRightCorner)

    def _build_body(self) -> None:
        central = QWidget(); self.setCentralWidget(central)
        outer = QVBoxLayout(central); outer.setContentsMargins(10, 10, 10, 6); outer.setSpacing(8)

        # ── top: previous / current / next / clock ──
        top = QHBoxLayout(); top.setSpacing(8)
        self.lbl_prev = self._track_label(COL_PREV)
        self.lbl_current = self._track_label(COL_CURRENT)
        self.lbl_next = self._track_label(COL_NEXT)
        self.lbl_clock = self._track_label(COL_TIME); self.lbl_clock.setMinimumWidth(210)
        f = QFont(); f.setPointSize(16); f.setBold(True); self.lbl_clock.setFont(f)
        for cap, w, stretch in (("Previous Track", self.lbl_prev, 3), ("Current Track", self.lbl_current, 3),
                                ("Next Track", self.lbl_next, 3), ("Time", self.lbl_clock, 2)):
            col = QVBoxLayout(); col.setSpacing(3)
            c = QLabel(cap); c.setObjectName("caption")
            col.addWidget(c); col.addWidget(w); top.addLayout(col, stretch)
        outer.addLayout(top)

        # ── middle: health+log (left) | operations (center) | running order (right) ──
        mid = QHBoxLayout(); mid.setSpacing(8)

        left = QVBoxLayout(); left.setSpacing(8)
        gb_health = QGroupBox("Gateway Health")
        hv = QVBoxLayout(gb_health)
        self.health = QTableWidget(len(self.HEALTH_ROWS), 2)
        self.health.horizontalHeader().setVisible(False)
        self.health.verticalHeader().setVisible(False)
        self.health.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.health.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.health.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.health.setShowGrid(False)
        self.health.verticalHeader().setDefaultSectionSize(30)
        self.health.horizontalHeader().setSectionResizeMode(0, _RESIZE.ResizeToContents)
        self.health.horizontalHeader().setSectionResizeMode(1, _RESIZE.Stretch)
        keyfont = QFont(); keyfont.setBold(True)
        for i, k in enumerate(self.HEALTH_ROWS):
            key = _item(k); key.setFont(keyfont); key.setForeground(QColor("#9fb2d8"))
            self.health.setItem(i, 0, key)
            self.health.setItem(i, 1, _item("—", color=NEUTRAL))
        hv.addWidget(self.health)
        left.addWidget(gb_health, 2)

        gb_log = QGroupBox("Log")
        lv = QVBoxLayout(gb_log)
        self.log = self._table(["Time", "Level", "Message"],
                               [_RESIZE.ResizeToContents, _RESIZE.ResizeToContents, _RESIZE.Stretch])
        lv.addWidget(self.log)
        left.addWidget(gb_log, 3)
        mid.addLayout(left, 4)

        self.tabs = QTabWidget()
        self.ops = self._table(["Time", "Operation"], [_RESIZE.ResizeToContents, _RESIZE.Stretch])
        self.tabs.addTab(self._wrap(self.ops), "Operations")
        # Schedule tab — today's approved playlist + a manifest/policy summary line.
        sched_w = QWidget(); sv = QVBoxLayout(sched_w); sv.setContentsMargins(0, 0, 0, 0); sv.setSpacing(4)
        self.manifest_lbl = QLabel("Approved content — syncing…")
        self.manifest_lbl.setStyleSheet("color:#9fb2d8; padding:2px;")
        self.schedule = self._table(["Time", "Title", "Type"],
                                    [_RESIZE.ResizeToContents, _RESIZE.Stretch, _RESIZE.ResizeToContents])
        sv.addWidget(self.manifest_lbl); sv.addWidget(self.schedule)
        self.tabs.addTab(sched_w, "Schedule")
        # Listener Messages — inbound SMS for the presenter to read on-air.
        self.messages = self._table(["Time", "Sender", "Message", "✓"],
                                    [_RESIZE.ResizeToContents, _RESIZE.ResizeToContents,
                                     _RESIZE.Stretch, _RESIZE.ResizeToContents])
        self.tabs.addTab(self._wrap(self.messages), "Listener Messages")
        mid.addWidget(self.tabs, 5)

        gb_ro = QGroupBox("Running Order")
        rv = QVBoxLayout(gb_ro)
        # Last column flags each track approved (✓) / unapproved (✕) vs the manifest.
        self.running = self._table(["#", "Title", "Artist", "Dur", "✓"],
                                   [_RESIZE.ResizeToContents, _RESIZE.Stretch, _RESIZE.Interactive,
                                    _RESIZE.ResizeToContents, _RESIZE.ResizeToContents])
        self.running.setColumnWidth(2, 120)
        rv.addWidget(self.running)
        mid.addWidget(gb_ro, 4)

        outer.addLayout(mid, 1)

    def _build_statusbar(self) -> None:
        # Left: audio monitor ("Listen" to the broadcast stream).
        self.listen_btn = QPushButton("  Listen")
        self.listen_btn.setCheckable(True)
        self.listen_btn.setStyleSheet("padding:2px 12px;")
        self.listen_btn.toggled.connect(self._toggle_monitor)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100); self.vol.setValue(80); self.vol.setFixedWidth(90)
        self.vol.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        if not (self.settings.monitor_stream_url or "").strip():
            self.listen_btn.setEnabled(False)
            self.listen_btn.setToolTip("Set GATEWAY_MONITOR_STREAM_URL in .env to enable stream monitoring")
        self.statusBar().addWidget(self.listen_btn)
        self.statusBar().addWidget(self.vol)

        # Right: service + uplink.
        self.svc_lbl = QLabel("Service: —")
        self.up_lbl = QLabel("Uplink: —")
        for w in (self.svc_lbl, self.up_lbl):
            w.setStyleSheet("padding:2px 12px; font-weight:600;")
        self.statusBar().addPermanentWidget(self.up_lbl)
        self.statusBar().addPermanentWidget(self.svc_lbl)

    # ── refresh ─────────────────────────────────────────────────────────
    def _tick_clock(self) -> None:
        self.lbl_clock.setText(datetime.now().strftime("%I:%M:%S %p"))
        if self._cue_overlay is not None:  # keep the T-minus countdown live every second
            self._cue_overlay.update_countdown()

    # ── must-air presenter cues ─────────────────────────────────────────
    def _update_cues(self, cues: list) -> None:
        """Show the most imminent active cue as a stay-on-top overlay. Cues the
        presenter already dismissed stay hidden; the daemon stops publishing a cue
        once its display window closes, so it clears on its own after air."""
        active = [c for c in cues if c.get("command_id") not in self._dismissed_cues]
        if not active:
            if self._cue_overlay is not None:
                self._cue_overlay.close()
                self._cue_overlay = None
            return

        cue = active[0]
        if self._cue_overlay is not None and self._cue_overlay.command_id == cue.get("command_id"):
            self._cue_overlay.update_countdown()
            return

        if self._cue_overlay is not None:
            self._cue_overlay.close()
        self._cue_overlay = CueOverlay(cue, on_dismiss=self._dismiss_cue, parent=self)
        self._cue_overlay.place(self)
        self._cue_overlay.show()

    def _dismiss_cue(self, command_id: str | None) -> None:
        if command_id:
            self._dismissed_cues.add(command_id)
        self._cue_overlay = None

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        worker = SnapshotWorker(self.data)
        worker.signals.done.connect(self._apply_snapshot)
        self.pool.start(worker)

    def _apply_snapshot(self, snap: dict) -> None:
        self._refreshing = False
        if snap.get("error"):
            self.statusBar().showMessage(f"Refresh error: {snap['error']}", 5000)
            return

        self._set_track(self.lbl_prev, snap.get("prev"))
        self._set_track(self.lbl_current, snap.get("current"))
        self._set_track(self.lbl_next, snap.get("next"))

        q = snap.get("queue") or {}
        rb_ok = bool(snap.get("radioboss_ok"))
        service = snap.get("service") or "Unknown"
        uplink = snap.get("uplink") or "—"
        pending, acks = q.get("pending", 0), q.get("acks", 0)

        values = [
            (service, OK if service == "Running" else BAD if service == "Stopped" else WARN),
            ("connected" if rb_ok else "unreachable", OK if rb_ok else BAD),
            (snap.get("state") or "—", NEUTRAL),
            (uplink, {"online": OK, "offline": WARN, "auth error": BAD}.get(uplink, NEUTRAL)),
            (str(pending), WARN if pending else NEUTRAL),
            (str(acks), WARN if acks else NEUTRAL),
        ]
        for row, (text, color) in enumerate(values):
            self.health.setItem(row, 1, _item(text, color=color))

        self._fill_running(snap.get("running_order") or [])
        self._fill_log(snap.get("log") or [])
        self._fill_ops(snap.get("log") or [])
        self._fill_messages(snap.get("messages") or [])
        self._update_cues(snap.get("cues") or [])

        self.svc_lbl.setText(f"Service: {service}")
        self.up_lbl.setText(f"Uplink: {uplink}")
        self.svc_lbl.setStyleSheet(f"padding:2px 12px;font-weight:600;color:"
                                   f"{OK if service == 'Running' else BAD if service == 'Stopped' else WARN}")
        self.up_lbl.setStyleSheet(f"padding:2px 12px;font-weight:600;color:"
                                  f"{OK if uplink == 'online' else BAD if uplink == 'auth error' else WARN}")

    # ── glanceable dashboards (ERP schedule + manifest) ─────────────────
    def refresh_glance(self) -> None:
        if self._glancing or self.settings.erp_base_url in ("", "(unset)"):
            return
        self._glancing = True
        worker = GlanceWorker(self.data)
        worker.signals.done.connect(self._apply_glance)
        self.pool.start(worker)

    def _apply_glance(self, g: dict) -> None:
        self._glancing = False
        if g.get("manifest_ok"):
            policy = g.get("policy") or {}
            auto = "on" if policy.get("auto_cut") else "off"
            self.manifest_lbl.setText(
                f"Approved assets: {g.get('approved_count', 0)}  ·  auto-cut: {auto}"
            )
        elif not g.get("schedule_ok"):
            self.manifest_lbl.setText("Approved content — server unreachable")
        self._fill_schedule(g.get("schedule") or [])

    def _fill_schedule(self, items: list) -> None:
        self.schedule.setRowCount(len(items))
        for i, it in enumerate(items):
            when = it.get("start_time") or it.get("time") or it.get("start") or ""
            title = it.get("title") or it.get("name") or os.path.basename(it.get("file") or "") or "—"
            typ = it.get("type") or ""
            self.schedule.setItem(i, 0, _item(str(when)))
            self.schedule.setItem(i, 1, _item(title))
            self.schedule.setItem(i, 2, _item(str(typ)))

    # ── audio monitoring ────────────────────────────────────────────────
    def _toggle_monitor(self, on: bool) -> None:
        url = (self.settings.monitor_stream_url or "").strip()
        if on:
            if not url:
                self.listen_btn.setChecked(False)
                return
            self.player.setSource(QUrl(url))
            self.player.play()
            self.listen_btn.setText("  Stop")
            self.statusBar().showMessage("Monitoring the broadcast stream…", 3000)
        else:
            self.player.stop()
            self.listen_btn.setText("  Listen")

    def _on_media_error(self, *_args) -> None:
        if self.player.error() != QMediaPlayer.Error.NoError:
            self.listen_btn.setChecked(False)
            self.listen_btn.setText("  Listen")
            self.statusBar().showMessage(
                f"Monitor error: {self.player.errorString() or 'stream unavailable'}", 6000)

    # ── diagnostics bundle ──────────────────────────────────────────────
    def _collect_diagnostics(self) -> None:
        import zipfile
        default = os.path.join(os.path.expanduser("~"), "gateway-diagnostics.zip")
        path, _ = QFileDialog.getSaveFileName(self, "Save Diagnostics", default, "Zip Archive (*.zip)")
        if not path:
            return
        data_dir = os.path.dirname(self.settings.db_path) or "."
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                if os.path.isdir(data_dir):
                    for f in sorted(os.listdir(data_dir)):
                        if f.startswith("gateway.log"):
                            z.write(os.path.join(data_dir, f), f)
                z.writestr("env.redacted.txt", self.data.redacted_env())
                z.writestr("summary.txt", self._diag_summary())
            self.statusBar().showMessage(f"Diagnostics saved → {path}", 8000)
            try:
                os.startfile(os.path.dirname(path))  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception as exc:
            self.statusBar().showMessage(f"Diagnostics failed: {exc}", 8000)

    def _diag_summary(self) -> str:
        s = self.settings
        return (
            f"MediaCore Studio Gateway — Console v{self._version()}\n"
            f"ERP base:     {s.erp_base_url}\n"
            f"Gateway id:   {s.gateway_id}\n"
            f"RadioBOSS:    {s.radioboss_base_url}\n"
            f"DB path:      {s.db_path}\n"
            f"Log:          {self.data.log_path()}\n"
        )

    def _fill_running(self, tracks: list) -> None:
        self.running.setRowCount(len(tracks))
        have_manifest = bool(self.data._manifest_fn)
        for i, t in enumerate(tracks):
            title = t.get("casttitle") or t.get("title") or os.path.basename(t.get("filename") or "") or "—"
            self.running.setItem(i, 0, _item(str(t.get("position", i + 1))))
            self.running.setItem(i, 1, _item(title))
            self.running.setItem(i, 2, _item(t.get("artist") or ""))
            self.running.setItem(i, 3, _item(t.get("duration") or ""))
            # Approved (✓) / unapproved (✕) vs the manifest — blank until it syncs.
            if not have_manifest:
                flag = _item("", "")
            elif t.get("approved"):
                flag = _item("✓", "Approved content", OK)
            else:
                flag = _item("✕", "Not in the approved manifest", BAD)
            flag.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.running.setItem(i, 4, flag)

    def _fill_log(self, rows: list) -> None:
        rows = rows[-200:]
        self.log.setRowCount(len(rows))
        for i, (t, lvl, msg) in enumerate(rows):
            self.log.setItem(i, 0, _item(t))
            color = WARN if lvl == "WARNING" else BAD if lvl in ("ERROR", "CRITICAL") else None
            self.log.setItem(i, 1, _item(lvl, color=color))
            self.log.setItem(i, 2, _item(msg))
        self.log.scrollToBottom()

    def _fill_ops(self, rows: list) -> None:
        keys = ("executed ", "leased ", "acked ", "auto-cut", "dead-lettered", "urgent", "synced asset", "published")
        ops = [(t, msg) for (t, _l, msg) in rows if any(k in msg for k in keys)][-100:]
        self.ops.setRowCount(len(ops))
        for i, (t, msg) in enumerate(ops):
            self.ops.setItem(i, 0, _item(t))
            self.ops.setItem(i, 1, _item(msg))
        self.ops.scrollToBottom()

    def _fill_messages(self, msgs: list) -> None:
        self.messages.setRowCount(len(msgs))
        for i, m in enumerate(msgs):
            when = m.get("received_at") or ""
            try:
                when = datetime.fromisoformat(when).strftime("%H:%M") if when else ""
            except (ValueError, TypeError):
                pass
            sender = m.get("name") or m.get("phone") or "—"
            self.messages.setItem(i, 0, _item(str(when)))
            self.messages.setItem(i, 1, _item(sender, m.get("phone") or ""))
            self.messages.setItem(i, 2, _item(m.get("content") or ""))
            # ✓ = registered as reach with MediaERP; ⋯ = still buffered
            posted = m.get("posted")
            flag = _item("✓", "Registered as reach", OK) if posted else _item("⋯", "Buffered — not yet sent", WARN)
            flag.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.messages.setItem(i, 3, flag)

    # ── helpers ─────────────────────────────────────────────────────────
    def _act(self, text, slot) -> QAction:
        a = QAction(text, self)
        if "\tF5" in text:
            a.setText(text.replace("\tF5", "")); a.setShortcut("F5")
        a.triggered.connect(lambda: slot())
        return a

    def _track_label(self, color: str) -> QLabel:
        lbl = QLabel("—"); lbl.setObjectName("track")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"#track {{ background:{color}; }}")
        lbl.setMinimumHeight(56); lbl.setWordWrap(False)
        return lbl

    def _set_track(self, lbl: QLabel, text: str | None) -> None:
        text = text or "—"
        lbl.setToolTip(text)
        fm = lbl.fontMetrics()
        lbl.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, max(60, lbl.width() - 16)))

    def _table(self, headers: list[str], modes: list | None = None) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(26)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ContiguousSelection)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.setWordWrap(False)
        t.setTextElideMode(Qt.TextElideMode.ElideRight)
        hdr = t.horizontalHeader()
        hdr.setHighlightSections(False)
        if modes:
            for i, m in enumerate(modes):
                hdr.setSectionResizeMode(i, m)
        else:
            hdr.setStretchLastSection(True)
        return t

    def _copy_selection(self) -> None:
        w = self.focusWidget()
        if not isinstance(w, QTableWidget):
            return
        rng = w.selectedRanges()
        if not rng:
            return
        lines = []
        for r in range(rng[0].topRow(), rng[0].bottomRow() + 1):
            cells = []
            for c in range(rng[0].leftColumn(), rng[0].rightColumn() + 1):
                it = w.item(r, c)
                cells.append(it.text() if it else "")
            lines.append("\t".join(cells))
        QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(f"Copied {len(lines)} row(s)", 2000)

    @staticmethod
    def _wrap(widget: QWidget) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(widget)
        return w

    @staticmethod
    def _open(path: str) -> None:
        try:
            if not os.path.splitext(path)[1]:
                os.makedirs(path, exist_ok=True)
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception:
            pass

    @staticmethod
    def _open_url(url: str) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(url))

    def _show_config(self) -> None:
        s = self.settings

        def mask(v: str) -> str:
            v = str(v or "")
            return "—" if not v or v == "(unset)" else (v[:2] + "•" * 6)

        text = (
            f"MediaERP URL:      {s.erp_base_url}\n"
            f"Gateway id:        {s.gateway_id}\n"
            f"Gateway key:       {mask(s.gateway_key)}\n\n"
            f"RadioBOSS URL:     {s.radioboss_base_url}\n"
            f"RadioBOSS pass:    {mask(s.radioboss_password)}\n"
            f"Insert position:   {s.insert_position}\n\n"
            f"Media folder:      {s.media_folder}\n"
            f"Schedule folder:   {s.schedule_folder}\n"
            f"Local DB:          {s.db_path}\n"
        )
        box = QMessageBox(self); box.setWindowTitle("Configuration")
        box.setText("Read-only view of the gateway configuration:")
        box.setInformativeText(text); box.exec()

    def _about(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("About")
        dlg.setStyleSheet(DARK_QSS)
        dlg.setMinimumWidth(380)
        lay = QVBoxLayout(dlg); lay.setContentsMargins(28, 22, 28, 20); lay.setSpacing(12)

        logo = QLabel(); logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pm = QPixmap(_res("wordmark_dark.png"))
        if not pm.isNull():
            logo.setPixmap(pm.scaledToWidth(300, Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(logo)

        tagline = QLabel("On Air. Online. In Control.")
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tagline.setStyleSheet(f"color:{COL_TAGLINE}; font-weight:700; font-size:11pt;")
        lay.addWidget(tagline)

        body = QLabel(
            f"MediaCore Studio Gateway — Console  ·  v{self._version()}\n\n"
            "Read-only operator console for the studio gateway service.\n"
            "The gateway daemon owns delivery + governance; this window observes it."
        )
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True); body.setStyleSheet("color:#cdd6e6;")
        lay.addWidget(body)

        btn = QPushButton("Close"); btn.setStyleSheet("padding:6px 22px;")
        btn.clicked.connect(dlg.accept)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(btn); row.addStretch()
        lay.addLayout(row)
        dlg.exec()

    @staticmethod
    def _version() -> str:
        try:
            from console import __version__
            return __version__
        except Exception:
            return "0.1.0"
