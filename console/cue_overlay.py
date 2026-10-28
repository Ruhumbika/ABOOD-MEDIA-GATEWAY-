"""Must-air presenter-cue overlay (Phase B).

A stay-on-top, frameless banner the studio presenter can't miss: the cue title,
the script to read (for live reads), and a live countdown to air. It is a pure
UX surface — dismissing it changes nothing on the server (the gateway already
acked delivery); it just clears the prompt locally.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

# OraMedia orange accent — the same alert colour used elsewhere in the console.
ACCENT = "#F08428"

OVERLAY_QSS = """
QDialog { background: #131a2b; border: 3px solid #F08428; border-radius: 10px; }
QLabel#kicker { color: #F08428; font-weight: 800; letter-spacing: 2px; }
QLabel#title  { color: #ffffff; font-weight: 800; }
QLabel#count  { color: #a9e39a; font-weight: 800; }
QLabel#script { color: #e6e9f0; background: #0f1524; border: 1px solid #2a3550;
                border-radius: 6px; padding: 10px; }
QPushButton { background: #23314f; color: #ffffff; border: 1px solid #2a3550;
              border-radius: 6px; padding: 8px 22px; font-weight: 700; }
QPushButton:hover { background: #2c4a7a; }
"""


class CueOverlay(QDialog):
    def __init__(self, cue: dict, on_dismiss, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.command_id = cue.get("command_id")
        self._air_at = self._parse(cue.get("air_at"))
        self._on_dismiss = on_dismiss

        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setModal(False)
        self.setStyleSheet(OVERLAY_QSS)
        self.setMinimumWidth(560)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 20, 26, 20)
        lay.setSpacing(12)

        kicker = QLabel("● MUST-AIR CUE")
        kicker.setObjectName("kicker")
        kf = QFont(); kf.setPointSize(11); kicker.setFont(kf)
        lay.addWidget(kicker)

        self.title = QLabel(cue.get("title") or "Must-air content")
        self.title.setObjectName("title")
        self.title.setWordWrap(True)
        tf = QFont(); tf.setPointSize(20); tf.setBold(True); self.title.setFont(tf)
        lay.addWidget(self.title)

        # Script block — only for live reads (asset carries copy to read).
        script = (cue.get("script") or "").strip()
        if script:
            read_lbl = QLabel("READ ON AIR:")
            read_lbl.setObjectName("kicker")
            lay.addWidget(read_lbl)
            self.script = QLabel(script)
            self.script.setObjectName("script")
            self.script.setWordWrap(True)
            sf = QFont(); sf.setPointSize(13); self.script.setFont(sf)
            lay.addWidget(self.script)

        row = QHBoxLayout()
        self.count = QLabel("—")
        self.count.setObjectName("count")
        cf = QFont(); cf.setPointSize(15); cf.setBold(True); self.count.setFont(cf)
        row.addWidget(self.count)
        row.addStretch()
        btn = QPushButton("Dismiss")
        btn.clicked.connect(self._dismiss)
        row.addWidget(btn)
        lay.addLayout(row)

        self.update_countdown()

    @staticmethod
    def _parse(iso: str | None) -> datetime | None:
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return None

    def update_countdown(self) -> None:
        """Refresh the T-minus / on-air label. Driven by the window's 1s clock."""
        if self._air_at is None:
            self.count.setText("Airing now")
            return
        now = datetime.now(timezone.utc)
        secs = int((self._air_at - now).total_seconds())
        if secs > 0:
            m, s = divmod(secs, 60)
            self.count.setText(f"On air in {m:d}:{s:02d}")
            self.count.setStyleSheet("color:#a9e39a;")
        else:
            self.count.setText("● ON AIR NOW")
            self.count.setStyleSheet(f"color:{ACCENT};")

    def _dismiss(self) -> None:
        if self._on_dismiss:
            self._on_dismiss(self.command_id)
        self.close()

    def place(self, parent: QWidget) -> None:
        """Centre the overlay over the parent window's top third."""
        pg = parent.frameGeometry()
        self.adjustSize()
        x = pg.center().x() - self.width() // 2
        y = pg.top() + max(40, pg.height() // 6)
        self.move(max(0, x), max(0, y))
