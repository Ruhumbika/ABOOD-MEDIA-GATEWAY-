"""Local integration fixture; not a production MediaERP replacement."""
import hashlib
import json
import math
import sqlite3
import struct
import uuid
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent / ".gateway-test"
ROOT.mkdir(exist_ok=True)
AUDIO = ROOT / "gateway-integration-test.wav"
KEY = "abood-local-test-only"

# Three-second quiet test tone. Inserting it does not start playback.
if not AUDIO.exists():
    with wave.open(str(AUDIO), "wb") as f:
        f.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
        f.writeframes(b"".join(
            struct.pack("<h", int(2500 * math.sin(2 * math.pi * 440 * n / 22050)))
            for n in range(22050 * 3)
        ))

CHECKSUM = hashlib.sha256(AUDIO.read_bytes()).hexdigest()
db = sqlite3.connect(ROOT / "mock.sqlite3")
db.execute("""CREATE TABLE IF NOT EXISTS command (
    id TEXT PRIMARY KEY, leased_until REAL DEFAULT 0, result TEXT
)""")
if db.execute("SELECT COUNT(*) FROM command").fetchone()[0] == 0:
    db.execute("INSERT INTO command(id) VALUES (?)", (str(uuid.uuid4()),))
    db.commit()


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if self.headers.get("X-Gateway-Key") == KEY:
            return True
        self.send_json({"error": "Invalid test gateway key"}, 401)
        return False

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self.send_json({"status": "ok", "mode": "local-test"})
        if not self.authorized():
            return
        if path == "/api/v1/gateway/files/1":
            body = AUDIO.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("X-Checksum", CHECKSUM)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/v1/gateway/schedule":
            self.send_json({"items": []})
        elif path == "/api/v1/gateway/manifest":
            self.send_json({
                "assets": [{
                    "asset_id": 1, "filename": AUDIO.name,
                    "checksum": CHECKSUM, "type": "JINGLE"
                }],
                "policy": {"auto_cut": False, "block_usb": False}
            })
        elif path == "/status":
            row = db.execute("SELECT id, result FROM command").fetchone()
            self.send_json({
                "command_id": row[0],
                "result": json.loads(row[1]) if row[1] else None
            })
        else:
            self.send_json({"error": "Unknown endpoint"}, 404)

    def do_POST(self):
        if not self.authorized():
            return
        path = urlsplit(self.path).path
        try:
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", "0"))
            ) or b"{}")
        except ValueError:
            return self.send_json({"error": "Invalid JSON"}, 400)

        if path == "/api/v1/gateway/lease":
            row = db.execute(
                "SELECT id FROM command WHERE result IS NULL AND leased_until <= ?",
                (time(),)
            ).fetchone()
            ops = []
            if row:
                db.execute("UPDATE command SET leased_until=? WHERE id=?",
                           (time() + 120, row[0]))
                db.commit()
                ops.append({
                    "command_id": row[0], "command_type": "inserttrack",
                    "priority": 0, "attempts": 0,
                    "payload": {
                        "asset_id": 1, "filename": AUDIO.name,
                        "checksum": CHECKSUM, "title": "Gateway Integration Test"
                    }
                })
            self.send_json({"operations": ops, "lease_ttl": 120})
        elif path == "/api/v1/gateway/ack":
            if (ROOT / "block-acks").exists():
                return self.send_json({"error": "Simulated ACK outage"}, 503)
            applied = 0
            for result in body.get("results", []):
                changed = db.execute(
                    "UPDATE command SET result=? WHERE id=? AND result IS NULL",
                    (json.dumps(result), result.get("command_id"))
                ).rowcount
                applied += changed
                print("ACK:", json.dumps(result), flush=True)
            db.commit()
            self.send_json({"applied": applied, "unknown": []})
        elif path == "/api/v1/gateway/violations":
            violations = body.get("violations", [])
            with (ROOT / "violations.jsonl").open("a") as f:
                for violation in violations:
                    f.write(json.dumps(violation) + "\n")
            print("VIOLATIONS:", len(violations), flush=True)
            self.send_json({"stored": len(violations)})
        elif path == "/api/v1/gateway/heartbeat":
            live = body.get("live") or {}
            print("HEARTBEAT:", live.get("state"), flush=True)
            self.send_json({"ok": True, "urgent": []})
        else:
            self.send_json({"error": "Unknown endpoint"}, 404)


print("Test server: http://192.168.122.1:8091", flush=True)
print("One persisted test command; restarting does not create another.", flush=True)
HTTPServer(("192.168.122.1", 8091), Handler).serve_forever()
