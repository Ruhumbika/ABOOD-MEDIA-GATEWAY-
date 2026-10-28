"""Local SQLite offline queue.

Leased work and un-posted acks are persisted here so the gateway keeps applying
commands to RadioBOSS and buffers results even while the uplink to MediaERP is
down — the studio never goes silent. Everything is keyed by command_id, so a
re-lease of the same command is idempotent.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work (
    command_id      TEXT PRIMARY KEY,
    command_type    TEXT NOT NULL,
    payload         TEXT,
    priority        INTEGER DEFAULT 0,
    attempts        INTEGER DEFAULT 0,
    spot_booking_id INTEGER,
    added_at        TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS acks (
    command_id  TEXT PRIMARY KEY,
    ok          INTEGER NOT NULL,
    confirmed   INTEGER,
    duration_ms INTEGER,
    response    TEXT,
    added_at    TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS presenter_cues (
    command_id    TEXT PRIMARY KEY,
    title         TEXT,
    script        TEXT,
    air_at        TEXT,        -- ISO-8601 when the must-air asset is scheduled
    display_until TEXT,        -- ISO-8601 after which the console stops showing it
    added_at      TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS listener_messages (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    phone       TEXT,
    name        TEXT,
    content     TEXT,
    received_at TEXT,
    posted      INTEGER DEFAULT 0,   -- 0 = not yet registered with MediaERP
    added_at    TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source, external_id)
);
"""


class LocalQueue:
    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)

    # ── work (leased, not yet executed) ────────────────────────────────
    def add_work(self, ops: list[dict]) -> int:
        added = 0
        for op in ops:
            command_id = op["command_id"]
            # Idempotency by command_id: the PRIMARY KEY covers a re-lease of a
            # command still sitting in `work`; this guard covers a re-lease of a
            # command that already EXECUTED and is only waiting for its ack to be
            # posted (the uplink dropped). Without it, that command would be
            # re-added and re-aired — a double-air. A server-side RETRY uses a
            # NEW command_id, so genuine retries (and repeated spots) still queue.
            if self._in_acks(command_id):
                continue
            cur = self.db.execute(
                "INSERT OR IGNORE INTO work "
                "(command_id, command_type, payload, priority, attempts, spot_booking_id) "
                "VALUES (?,?,?,?,?,?)",
                (
                    command_id,
                    op["command_type"],
                    json.dumps(op.get("payload") or {}),
                    int(op.get("priority") or 0),
                    int(op.get("attempts") or 0),
                    op.get("spot_booking_id"),
                ),
            )
            added += cur.rowcount
        return added

    def _in_acks(self, command_id: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM acks WHERE command_id=?", (command_id,)).fetchone()
            is not None
        )

    def bump_attempt(self, command_id: str) -> None:
        """Count one execution attempt. Persisted before the RadioBOSS call so a
        crash mid-execute is visible on restart (drives dead-lettering and the
        re-attempt idempotency pre-check)."""
        self.db.execute("UPDATE work SET attempts=attempts+1 WHERE command_id=?", (command_id,))

    def pending_work(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM work ORDER BY priority DESC, added_at ASC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
            result.append(d)
        return result

    def mark_executed(self, command_id: str, result: dict) -> None:
        """Move a work item to the ack buffer atomically."""
        self.db.execute("BEGIN")
        try:
            self.db.execute("DELETE FROM work WHERE command_id=?", (command_id,))
            self.db.execute(
                "INSERT OR REPLACE INTO acks (command_id, ok, confirmed, duration_ms, response) "
                "VALUES (?,?,?,?,?)",
                (
                    command_id,
                    1 if result["ok"] else 0,
                    None if result.get("confirmed") is None else (1 if result["confirmed"] else 0),
                    int(result.get("duration_ms") or 0),
                    result.get("response"),
                ),
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # ── acks (executed, awaiting post to MediaERP) ─────────────────────
    def pending_acks(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM acks ORDER BY added_at ASC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            out.append(
                {
                    "command_id": d["command_id"],
                    "ok": bool(d["ok"]),
                    "confirmed": None if d["confirmed"] is None else bool(d["confirmed"]),
                    "duration_ms": d["duration_ms"],
                    "response": d["response"],
                }
            )
        return out

    def clear_acks(self, command_ids: list[str]) -> None:
        if not command_ids:
            return
        placeholders = ",".join("?" for _ in command_ids)
        self.db.execute(f"DELETE FROM acks WHERE command_id IN ({placeholders})", command_ids)

    def depth(self) -> tuple[int, int]:
        """(buffered work, pending acks) — the offline-queue depth for telemetry."""
        work = self.db.execute("SELECT COUNT(*) FROM work").fetchone()[0]
        acks = self.db.execute("SELECT COUNT(*) FROM acks").fetchone()[0]
        return int(work), int(acks)

    def has_command(self, command_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM work WHERE command_id=? UNION SELECT 1 FROM acks WHERE command_id=?",
            (command_id, command_id),
        ).fetchone()
        return row is not None

    # ── listener messages (inbound SMS → presenter + reach) ─────────────
    def add_messages(self, msgs: list[dict]) -> int:
        """Buffer newly-read SMS. Idempotent by (source, external_id)."""
        added = 0
        for m in msgs:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO listener_messages "
                "(source, external_id, phone, name, content, received_at) VALUES (?,?,?,?,?,?)",
                (m.get("source") or "SMS", str(m.get("external_id")), m.get("phone"),
                 m.get("name"), m.get("content"), m.get("received_at")),
            )
            added += cur.rowcount
        return added

    def unposted_messages(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT source, external_id, phone, name, content, received_at "
            "FROM listener_messages WHERE posted=0 ORDER BY added_at ASC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_posted(self, keys: list[tuple[str, str]]) -> None:
        for source, external_id in keys:
            self.db.execute(
                "UPDATE listener_messages SET posted=1 WHERE source=? AND external_id=?",
                (source, external_id),
            )

    def recent_messages(self, limit: int = 100) -> list[dict]:
        """Newest-first, for the console's presenter panel."""
        rows = self.db.execute(
            "SELECT source, external_id, phone, name, content, received_at, posted "
            "FROM listener_messages ORDER BY COALESCE(received_at, added_at) DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── presenter cues (must-air pre-roll → console overlay) ────────────
    def add_cue(self, cue: dict) -> int:
        """Store a presenter cue for the console overlay to render. Idempotent by
        command_id, so a re-lease of the same cue doesn't duplicate the prompt."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO presenter_cues "
            "(command_id, title, script, air_at, display_until) VALUES (?,?,?,?,?)",
            (cue["command_id"], cue.get("title"), cue.get("script"),
             cue.get("air_at"), cue.get("display_until")),
        )
        return cur.rowcount

    def prune_cues(self, before_iso: str) -> int:
        """Drop cues whose display window closed before `before_iso` (housekeeping
        so the table doesn't grow without bound)."""
        cur = self.db.execute(
            "DELETE FROM presenter_cues WHERE display_until IS NOT NULL AND display_until < ?",
            (before_iso,),
        )
        return cur.rowcount

    def close(self) -> None:
        self.db.close()
