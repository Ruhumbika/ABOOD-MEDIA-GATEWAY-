"""Persist worker heartbeat outcomes for the read-only operator console."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import time

log = logging.getLogger("gateway.uplink")


def heartbeat_scope(settings) -> str:
    """Bind health to this ERP/station configuration without storing its key."""
    identity = [settings.erp_base_url.rstrip("/"), settings.gateway_id, settings.gateway_key]
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


def record_heartbeat(db: sqlite3.Connection, scope: str, error=None, *, now=None) -> None:
    """Record only a completed heartbeat; unrelated endpoint failures do not count."""
    checked_at = time.time() if now is None else now
    outcome = "online" if error is None else "offline"
    if error is not None and re.search(r"\bHTTP\s+(401|403)\b", str(error), re.I):
        outcome = "auth error"
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS gateway_uplink (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            scope TEXT NOT NULL, outcome TEXT NOT NULL, checked_at REAL NOT NULL
        )""")
        db.execute("""INSERT INTO gateway_uplink(id, scope, outcome, checked_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET scope=excluded.scope,
                outcome=excluded.outcome, checked_at=excluded.checked_at
        """, (scope, outcome, checked_at))
    except sqlite3.Error:
        # Health reporting must not discard urgent commands or stop playout.
        log.warning("could not persist heartbeat status", exc_info=True)


def read_uplink(db_path: str, scope: str, *, max_age=30.0, now=None) -> str:
    """Unknown until a heartbeat exists; stale outcomes become offline."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            row = db.execute(
                "SELECT scope, outcome, checked_at FROM gateway_uplink WHERE id=1"
            ).fetchone()
        finally:
            db.close()
        if row is None or row[0] != scope:
            return "—"
        outcome, checked_at = row[1], float(row[2])
        if outcome not in ("online", "offline", "auth error") or not math.isfinite(checked_at):
            return "—"
        age = (time.time() if now is None else now) - checked_at
        if age < -5.0 or age > max_age:
            return "offline"
        return outcome
    except (sqlite3.Error, OSError, ValueError, TypeError):
        # Includes old databases before the upgraded worker has run.
        return "—"
