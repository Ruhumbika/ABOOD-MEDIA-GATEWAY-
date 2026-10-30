"""RadioBOSS history observations and a durable, explicitly acknowledged outbox.

A history observation is evidence of a playout start, not completed audio or RF.
No scheduled item or successful command is ever converted into a play event here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import ntpath
from pathlib import Path
import sqlite3
import time

from .erp_client import ErpError

log = logging.getLogger("gateway.proof_of_play")


def event_scope(settings) -> str:
    # Key rotation keeps the outbox; changing station/server does not reroute it.
    identity = [settings.erp_base_url.rstrip("/"), settings.gateway_id,
                settings.radioboss_base_url.rstrip("/")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


class PlayOutbox:
    def __init__(self, db: sqlite3.Connection, scope: str, gateway_id: str,
                 source_timezone: str = "") -> None:
        self.db, self.scope, self.gateway_id = db, scope, gateway_id
        self.source_timezone = source_timezone
        db.execute("""CREATE TABLE IF NOT EXISTS play_events (
            scope TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL,
            observed_at REAL NOT NULL, sent_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(scope, event_id)
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS play_events_pending
            ON play_events(scope, sent_at, next_attempt)""")

    def capture(self, tracks: list[dict], *, now: float | None = None) -> tuple[int, int]:
        now = time.time() if now is None else now
        added = skipped = 0
        # Each insert commits independently on the worker's autocommit connection.
        # A crash during a poll is safe: the next poll deduplicates all old rows.
        for track in tracks:
            filename = track.get("filename") or ""
            started = track.get("start_time") or ""
            try:
                datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
                if not filename:
                    raise ValueError("missing filename")
            except (ValueError, TypeError):
                skipped += 1
                continue
            identity = [self.scope, ntpath.normcase(ntpath.normpath(filename)), started]
            event_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            payload = {
                "event_id": event_id, "gateway_id": self.gateway_id,
                "source": "radioboss.getlastplayed", "evidence": "playout_start_observed",
                "started_at_local": started, "source_timezone": self.source_timezone or None,
                "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "filename": filename, "title": track.get("title") or "",
                "artist": track.get("artist") or "", "casttitle": track.get("casttitle") or "",
                "source_duration": track.get("duration") or "",
            }
            cur = self.db.execute("""INSERT OR IGNORE INTO play_events
                (scope,event_id,payload,observed_at) VALUES (?,?,?,?)""",
                (self.scope, event_id, json.dumps(payload), now))
            added += cur.rowcount
        return added, skipped

    def flush(self, erp, endpoint: str, *, now: float | None = None) -> int:
        if not endpoint:
            return 0
        now = time.time() if now is None else now
        rows = self.db.execute("""SELECT event_id,payload,attempts FROM play_events
            WHERE scope=? AND sent_at IS NULL AND next_attempt<=?
            ORDER BY observed_at,event_id LIMIT 50""", (self.scope, now)).fetchall()
        if not rows:
            return 0
        try:
            accepted = erp.report_play_events(endpoint, [json.loads(r[1]) for r in rows])
            # Defence in depth: only ids in this batch may be acknowledged.
            if not isinstance(accepted, list) or not all(isinstance(x, str) for x in accepted):
                raise ErpError("invalid play-event acknowledgement")
            accepted = set(accepted) & {r[0] for r in rows}
            error = "event not acknowledged"
        except ErpError:
            accepted = set()
            # Never persist exception URLs, keys, or response bodies.
            error = "delivery failed; check endpoint, authentication and connection"
        for event_id, _payload, attempts in rows:
            if event_id in accepted:
                self.db.execute("""UPDATE play_events SET sent_at=?,attempts=attempts+1,
                    last_error='' WHERE scope=? AND event_id=?""", (now, self.scope, event_id))
            else:
                delay = min(300, 5 * 2 ** min(attempts, 6))
                self.db.execute("""UPDATE play_events SET attempts=attempts+1,
                    next_attempt=?,last_error=? WHERE scope=? AND event_id=?""",
                    (now + delay, error, self.scope, event_id))
        if error and len(accepted) < len(rows):
            log.warning("play-event delivery: %s (%s pending in batch)", error, len(rows)-len(accepted))
        return len(accepted)


def read_play_events(db_path: str, scope: str) -> dict:
    """Read-only, compatible with old/missing databases. Never creates a file."""
    out = {"pending": 0, "sent": 0, "events": [], "available": False}
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            counts = db.execute("""SELECT COUNT(*),COUNT(sent_at) FROM play_events
                WHERE scope=?""", (scope,)).fetchone()
            out.update(pending=counts[0]-counts[1], sent=counts[1], available=True)
            rows = db.execute("""SELECT payload,sent_at,attempts,last_error FROM play_events
                WHERE scope=? ORDER BY observed_at DESC,event_id DESC LIMIT 100""", (scope,)).fetchall()
            out["events"] = [dict(json.loads(p), delivery="Sent" if sent is not None else "Pending",
                                  attempts=a, last_error=e) for p,sent,a,e in rows]
        finally:
            db.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        pass
    return out
