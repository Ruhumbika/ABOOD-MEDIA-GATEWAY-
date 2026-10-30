"""Read-only data adapters for the console.

Everything here is safe to call from a background thread and never mutates
gateway state — the console observes, it doesn't command. Data comes from the
same modules the daemon uses (config, RadioBoss) plus a read-only peek at the
local queue DB and the log file.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from collections import deque
from datetime import date, datetime, timezone

from gateway.config import Settings, _find_env_file, load_settings
from gateway.erp_client import ErpClient
from gateway.proof_of_play import event_scope, read_play_events
from gateway.radioboss import RadioBoss
from gateway.uplink import heartbeat_scope, read_uplink

SERVICE_ID = "mediacore-studio-gateway"


def load_console_settings() -> tuple[Settings, str | None]:
    """Load real settings; on a missing/incomplete .env, fall back to something
    usable so the window still opens (with a warning) instead of exiting."""
    try:
        return load_settings(), None
    except SystemExit:
        try:
            s = Settings(_env_file=_find_env_file(), erp_base_url="(unset)", gateway_key="(unset)")  # type: ignore[call-arg]
        except Exception:
            s = Settings(erp_base_url="(unset)", gateway_key="(unset)")
        return s, "Gateway not fully configured — .env missing or incomplete."


def _fmt_track(t: dict | None) -> str:
    if not t:
        return ""
    title = t.get("title") or t.get("casttitle") or os.path.basename(t.get("filename") or "") or ""
    artist = t.get("artist") or ""
    return f"{artist} — {title}" if artist and title else (title or artist)


def _is_log_head(line: str) -> bool:
    """True if the line starts a new log record: 'YYYY-MM-DD HH:MM:SS,mmm LEVEL ...'."""
    if len(line) < 20 or line[4] != "-" or line[7] != "-":
        return False
    return line[:4].isdigit()


def tail_log(path: str, n: int = 200) -> list[tuple[str, str, str]]:
    """Return the last n log RECORDS as (time, level, message).

    Python logging writes multi-line records (e.g. a pretty-printed JSON error
    body or a traceback). Continuation lines are folded into their parent record
    so the table shows one row per event, not a wall of orphaned fragments.
    """
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = deque(fh, maxlen=n * 6)  # over-read: records span several lines
    except OSError:
        return []
    rows: list[list[str]] = []
    for raw in lines:
        ln = raw.rstrip("\n")
        if _is_log_head(ln):
            parts = ln.split(" ", 3)
            msg = parts[3] if len(parts) >= 4 else ""
            rows.append([parts[1].split(",")[0], parts[2], msg])
        elif ln.strip():
            if rows:
                rows[-1][2] = (rows[-1][2] + " " + ln.strip())[:2000]
            else:
                rows.append(["", "", ln.strip()])
    return [tuple(r) for r in rows[-n:]]


def service_status() -> str:
    try:
        out = subprocess.run(
            ["sc", "query", SERVICE_ID],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = out.stdout or ""
        if "RUNNING" in text:
            return "Running"
        if "STOPPED" in text:
            return "Stopped"
        if "START_PENDING" in text or "STOP_PENDING" in text:
            return "Pending"
        return "Not installed"
    except Exception:
        return "Unknown"


class GatewayData:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.rb = RadioBoss(
            settings.radioboss_base_url,
            settings.radioboss_password,
            settings.radioboss_user,
            settings.radioboss_timeout,
            settings.insert_position,
        )
        self._last_current: str | None = None
        self._prev_track: str = ""

        # Read-only ERP link for glanceable dashboards (today's schedule +
        # approved-content manifest). The console uses the station's own key.
        self.erp = ErpClient(settings.erp_base_url, settings.gateway_key,
                             settings.gateway_id, settings.http_timeout)
        self._manifest_fn: dict[str, str] = {}  # basename(lower) → asset type

    def log_path(self) -> str:
        return os.path.join(os.path.dirname(self.s.db_path) or ".", "gateway.log")

    def _queue_counts(self) -> dict:
        out = {"pending": 0, "acks": 0}
        path = self.s.db_path
        if not os.path.isfile(path):
            return out
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
            try:
                out["pending"] = con.execute("SELECT COUNT(*) FROM work").fetchone()[0]
                out["acks"] = con.execute("SELECT COUNT(*) FROM acks").fetchone()[0]
            finally:
                con.close()
        except Exception:
            pass
        return out

    def _recent_messages(self, limit: int = 60) -> list[dict]:
        """Newest-first listener SMS the service buffered (read-only)."""
        path = self.s.db_path
        if not os.path.isfile(path):
            return []
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
            try:
                rows = con.execute(
                    "SELECT phone, name, content, received_at, posted FROM listener_messages "
                    "ORDER BY COALESCE(received_at, added_at) DESC LIMIT ?", (limit,)
                ).fetchall()
                return [{"phone": r[0], "name": r[1], "content": r[2],
                         "received_at": r[3], "posted": r[4]} for r in rows]
            finally:
                con.close()
        except Exception:
            return []  # table may not exist yet (older service build)

    def _active_cues(self) -> list[dict]:
        """Must-air presenter cues whose display window is still open (read-only).
        Newest air time first. Tolerates an older service build without the table."""
        path = self.s.db_path
        if not os.path.isfile(path):
            return []
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
            try:
                rows = con.execute(
                    "SELECT command_id, title, script, air_at, display_until "
                    "FROM presenter_cues "
                    "WHERE display_until IS NULL OR display_until >= ? "
                    "ORDER BY air_at ASC", (now_iso,)
                ).fetchall()
                return [{"command_id": r[0], "title": r[1], "script": r[2],
                         "air_at": r[3], "display_until": r[4]} for r in rows]
            finally:
                con.close()
        except Exception:
            return []  # table may not exist yet (older service build)

    def snapshot(self) -> dict:
        """One consistent read of everything the UI shows. Runs off the UI thread."""
        playback = self.rb.playback_info()
        current = _fmt_track((playback or {}).get("current"))
        nxt = _fmt_track((playback or {}).get("next"))

        # Track the previous song by remembering the last distinct 'current'.
        if current and current != self._last_current:
            self._prev_track = self._last_current or ""
            self._last_current = current

        # Tag each running-order track approved/unapproved against the last-synced
        # manifest (identity by basename — same rule the daemon's classifier uses).
        running = self.rb.playlist()
        mfn = self._manifest_fn
        for t in running:
            base = os.path.basename(t.get("filename") or "").lower()
            t["approved"] = bool(base and base in mfn)

        return {
            "prev": self._prev_track,
            "current": current,
            "next": nxt,
            "state": (playback or {}).get("state"),
            "radioboss_ok": playback is not None,
            "running_order": running,
            "queue": self._queue_counts(),
            "log": tail_log(self.log_path(), 200),
            "service": service_status(),
            "uplink": read_uplink(
                self.s.db_path, heartbeat_scope(self.s),
                max_age=max(30.0, 3.0 * self.s.heartbeat_interval),
            ),
            "play_history": dict(read_play_events(self.s.db_path, event_scope(self.s)),
                                 enabled=self.s.proof_of_play_enabled,
                                 delivery_enabled=bool(self.s.proof_of_play_endpoint)),
            "messages": self._recent_messages(),
            "cues": self._active_cues(),
        }

    def fetch_glance(self) -> dict:
        """Slower ERP read for the dashboards: today's approved schedule + the
        approved-content manifest summary. Updates the manifest map used to
        classify the running order. Safe on a background thread."""
        out: dict = {"schedule": [], "approved_count": 0, "policy": {}, "manifest_ok": False, "schedule_ok": False}
        try:
            mani = self.erp.get_manifest()
            assets = mani.get("assets", []) or []
            fn: dict[str, str] = {}
            for a in assets:
                f = a.get("filename")
                if f:
                    fn[os.path.basename(f).lower()] = a.get("type")
            self._manifest_fn = fn  # atomic reference swap — safe cross-thread
            out["approved_count"] = len(assets)
            out["policy"] = mani.get("policy") or {}
            out["manifest_ok"] = True
        except Exception:
            pass
        try:
            sched = self.erp.get_schedule(date.today().isoformat())
            out["schedule"] = sched.get("items", []) or []
            out["schedule_ok"] = True
        except Exception:
            pass
        return out

    def redacted_env(self) -> str:
        """The active .env with secrets masked — for the diagnostics bundle."""
        path = _find_env_file()
        if not path or not os.path.isfile(path):
            return "(no .env found)"
        lines: list[str] = []
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                for raw in fh:
                    ln = raw.rstrip("\n")
                    if "=" in ln and any(k in ln.upper() for k in ("KEY", "PASSWORD", "SECRET")):
                        lines.append(ln.split("=", 1)[0] + "=***REDACTED***")
                    else:
                        lines.append(ln)
        except OSError as exc:
            return f"(could not read .env: {exc})"
        return "\n".join(lines)
