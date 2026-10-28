"""Inbound listener SMS ingest (studio-local) — RadiQ's presenter feature, rebuilt.

Two selectable sources (`GATEWAY_SMS_SOURCE`):

  - 'adb'  — a phone connected via ADB (USB/Bluetooth); read content://sms/inbox
             with a bundled adb.exe. Works where the shell user may read SMS
             (older Android / permissive devices).
  - 'http' — the robust, modern path: an off-the-shelf Android SMS-forwarding app
             (SMSSync, "SMS to URL Forwarder", android-sms-gateway webhook,
             Tasker, …) is configured to POST each incoming SMS to the gateway.
             The gateway LISTENS on host:port; the phone pushes to it over the
             LAN (or via `adb reverse` for USB-only).

…so the presenter can read listener messages on-air and each sender is
registered as audience reach in MediaERP.

Each source yields NEW messages as dicts:
    {"external_id", "phone", "name", "content", "received_at"}   # received_at ISO8601
De-dup is by external_id.
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("gateway.sms")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _epoch_ms_to_iso(ms: str) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return datetime.now(tz=timezone.utc).isoformat()


def _first(data: dict, *keys: str):
    """First present, non-empty value among keys (case-insensitive)."""
    low = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def _norm_time(v) -> str:
    if not v:
        return datetime.now(tz=timezone.utc).isoformat()
    s = str(v)
    if s.isdigit():
        n = int(s)
        if n > 10 ** 12:  # milliseconds → seconds
            n //= 1000
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            pass
    return s  # assume already a date string


class SmsReader:
    source = "SMS"

    def poll(self) -> list[dict]:  # pragma: no cover - interface
        return []

    def close(self) -> None:  # pragma: no cover - interface
        pass


# ── ADB (phone over USB/Bluetooth) ──────────────────────────────────────
def _bundled_adb() -> str | None:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for cand in (os.path.join(base, "gateway", "bin", "adb", "adb.exe"),
                 os.path.join(base, "bin", "adb", "adb.exe")):
        if os.path.isfile(cand):
            return cand
    return None


class AdbSmsReader(SmsReader):
    """Query the connected phone's SMS inbox via ADB. Requires USB debugging and
    a device where the shell user can read the SMS provider."""
    source = "SMS"
    # Row: 0 _id=42, address=+255700..., date=1699999999000, body=Hello there
    _ROW = re.compile(r"_id=(\d+),\s*address=(.*?),\s*date=(\d+),\s*body=(.*)$")

    def __init__(self, settings) -> None:
        self.adb = (settings.sms_adb_path or "").strip() or _bundled_adb() or "adb"
        self.device = (settings.sms_adb_device or "").strip()
        self._last_id = 0

    def _base_cmd(self) -> list[str]:
        cmd = [self.adb]
        if self.device:
            cmd += ["-s", self.device]
        return cmd

    def poll(self) -> list[dict]:
        args = self._base_cmd() + [
            "shell", "content", "query", "--uri", "content://sms/inbox",
            "--projection", "_id:address:date:body",
        ]
        if self._last_id:
            args += ["--where", f'"_id>{self._last_id}"']
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=20,
                                 creationflags=_NO_WINDOW)
        except Exception as exc:
            log.warning("adb sms query failed: %s", exc)
            return []
        if out.returncode != 0:
            log.warning("adb sms query rc=%s: %s", out.returncode, (out.stderr or "").strip()[:200])
            return []
        return self.parse(out.stdout or "")

    def parse(self, text: str) -> list[dict]:
        msgs: list[dict] = []
        for line in text.splitlines():
            m = self._ROW.search(line)
            if not m:
                continue
            sms_id, address, date_ms, body = m.group(1), m.group(2).strip(), m.group(3), m.group(4).strip()
            self._last_id = max(self._last_id, int(sms_id))
            if not address:
                continue
            msgs.append({
                "external_id": sms_id,
                "phone": address,
                "name": None,
                "content": body,
                "received_at": _epoch_ms_to_iso(date_ms),
            })
        return msgs


# ── HTTP receiver (off-the-shelf forwarding app pushes SMS) ─────────────
def _parse_push(raw: bytes, ctype: str) -> dict | None:
    """Normalise a pushed SMS body (JSON or form-encoded) into our message dict.
    Tolerant of the many field names different apps use."""
    text = raw.decode("utf-8", "replace")
    data: dict = {}
    try:
        if "json" in (ctype or "").lower():
            data = _json.loads(text or "{}")
        else:
            form = parse_qs(text)
            data = {k: v[0] for k, v in form.items()} if form else _json.loads(text or "{}")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    phone = _first(data, "from", "sender", "phone", "number", "address", "msisdn", "originator")
    if not phone:
        return None
    content = _first(data, "message", "text", "body", "content", "msg", "message_body") or ""
    received = _first(data, "received_at", "timestamp", "sentStamp", "date", "time", "sent")
    ext = _first(data, "id", "message_id", "messageId", "messageID", "uuid")
    ext = str(ext) if ext else "http:" + hashlib.sha1(f"{phone}|{content}|{received or ''}".encode()).hexdigest()[:16]
    return {
        "external_id": ext,
        "phone": str(phone),
        "name": _first(data, "name", "contact_name", "contact"),
        "content": content,
        "received_at": _norm_time(received),
    }


class HttpSmsReceiver(SmsReader):
    """Listen for inbound SMS PUSHED by an Android SMS-forwarding app.

    The app is configured to POST each incoming SMS to
    http://<studio-pc>:<port>/  (any path). poll() drains what's arrived.
    """
    source = "SMS"

    def __init__(self, settings) -> None:
        self.bind = (settings.sms_http_bind or "0.0.0.0:8099").strip()
        self.token = (settings.sms_http_token or "").strip()
        self._buf: list[dict] = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._start()

    def _start(self) -> None:
        host, _, port_s = self.bind.partition(":")
        host = host or "0.0.0.0"
        port = int(port_s or 8099)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):  # silence default stderr logging
                pass

            def _authorised(self) -> bool:
                if not receiver.token:
                    return True
                q = parse_qs(urlparse(self.path).query)
                tok = (q.get("token", [""])[0]) or self.headers.get("X-Token", "")
                return tok == receiver.token

            def do_POST(self):  # noqa: N802
                if not self._authorised():
                    self.send_response(401); self.end_headers(); return
                n = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(n) if n else b""
                msg = _parse_push(raw, self.headers.get("Content-Type", ""))
                if msg:
                    with receiver._lock:
                        receiver._buf.append(msg)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        try:
            self._server = ThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            log.error("SMS HTTP receiver could not bind %s:%d — %s", host, port, exc)
            return
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="sms-http")
        self._thread.start()
        log.info("SMS HTTP receiver listening on %s:%d (token=%s)", host, port, "set" if self.token else "none")

    def poll(self) -> list[dict]:
        with self._lock:
            out, self._buf = self._buf, []
        return out

    def close(self) -> None:
        try:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
        except Exception:
            pass
        self._server = None


def make_sms_reader(settings) -> SmsReader | None:
    """Build the configured reader, or None when SMS ingest is disabled."""
    src = (settings.sms_source or "").strip().lower()
    if src == "adb":
        return AdbSmsReader(settings)
    if src in ("http", "gsm", "api"):   # 'gsm'/'api' kept as RadiQ-familiar aliases
        return HttpSmsReceiver(settings)
    if src:
        log.warning("unknown GATEWAY_SMS_SOURCE=%r (use 'adb' or 'http')", src)
    return None
