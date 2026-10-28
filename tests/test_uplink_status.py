"""Run from the project root: python tests/test_uplink_status.py."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.queue import LocalQueue
from gateway.uplink import heartbeat_scope, read_uplink, record_heartbeat


class UplinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "health # test.db")
        self.q = LocalQueue(self.path)
        self.s = SimpleNamespace(erp_base_url="https://erp.test/api/v1",
                                 gateway_id="studio-1", gateway_key="secret",
                                 heartbeat_interval=0)
        self.scope = heartbeat_scope(self.s)

    def tearDown(self):
        self.q.close()
        self.tmp.cleanup()

    def read(self, now=100):
        return read_uplink(self.path, self.scope, now=now)

    def record(self, error=None, now=100):
        record_heartbeat(self.q.db, self.scope, error, now=now)

    def test_old_database_is_unknown_and_not_migrated_by_reader(self):
        self.assertEqual(self.read(), "—")
        self.assertIsNone(self.q.db.execute(
            "SELECT name FROM sqlite_master WHERE name='gateway_uplink'"
        ).fetchone())

    def test_missing_database_is_not_created(self):
        path = os.path.join(self.tmp.name, "absent.db")
        self.assertEqual(read_uplink(path, self.scope), "—")
        self.assertFalse(Path(path).exists())

    def test_success_and_expiry(self):
        self.record()
        self.assertEqual(self.read(130), "online")
        self.assertEqual(self.read(131), "offline")

    def test_configurable_freshness(self):
        self.record()
        self.assertEqual(read_uplink(self.path, self.scope, now=145, max_age=60), "online")

    def test_failure_then_recovery(self):
        self.record()
        self.record(RuntimeError("RemoteDisconnected"), now=101)
        self.assertEqual(self.read(101), "offline")
        self.record(now=102)
        self.assertEqual(self.read(102), "online")

    def test_auth_failure_then_recovery(self):
        for code in (401, 403):
            self.record(RuntimeError(f"gateway/heartbeat → HTTP {code}: denied"))
            self.assertEqual(self.read(), "auth error")
        self.record()
        self.assertEqual(self.read(), "online")

    def test_stale_auth_failure_becomes_offline(self):
        self.record(RuntimeError("HTTP 401"))
        self.assertEqual(self.read(131), "offline")

    def test_future_timestamp_is_not_online(self):
        self.record(now=200)
        self.assertEqual(self.read(100), "offline")

    def test_configuration_change_does_not_reuse_old_status(self):
        self.record()
        self.s.gateway_key = "new-secret"
        self.assertEqual(read_uplink(self.path, heartbeat_scope(self.s), now=100), "—")
        self.s.gateway_key = "secret"
        self.s.erp_base_url += "/"
        self.assertEqual(heartbeat_scope(self.s), self.scope)
        self.s.gateway_id = "another-studio"
        self.assertEqual(read_uplink(self.path, heartbeat_scope(self.s), now=100), "—")
        self.s.gateway_id = "studio-1"
        self.s.erp_base_url = "https://another-erp.test/api/v1"
        self.assertEqual(read_uplink(self.path, heartbeat_scope(self.s), now=100), "—")

    def test_persistence_across_connections(self):
        self.record()
        self.q.close()
        self.q = LocalQueue(self.path)
        self.assertEqual(self.read(), "online")

    def test_recording_does_not_change_work_or_acks(self):
        self.q.add_work([{"command_id": "queued", "command_type": "command"}])
        before = self.q.depth()
        self.record()
        self.assertEqual(self.q.depth(), before)

    def test_secrets_and_error_body_are_not_persisted(self):
        self.record(RuntimeError("HTTP 401 secret-password"))
        row = self.q.db.execute("SELECT * FROM gateway_uplink").fetchone()
        self.assertNotIn("secret", str(tuple(row)))

    def test_worker_heartbeat_success_failure_and_recovery(self):
        from gateway.erp_client import ErpError
        from gateway.worker import Worker
        worker = Worker.__new__(Worker)
        worker.s, worker.q = self.s, self.q
        worker._last_heartbeat = 0
        worker.rb = SimpleNamespace(playback_info=lambda: None)
        worker._telemetry = lambda: {}
        worker._report_dead_air = lambda audio: None
        worker._flush_recordings = lambda: None
        worker.erp = SimpleNamespace(heartbeat=lambda **kw: {"urgent": []})
        worker._maybe_heartbeat()
        self.assertEqual(read_uplink(self.path, self.scope), "online")

        def rejected(**kw):
            raise ErpError("gateway/heartbeat → HTTP 401: denied")

        worker.erp.heartbeat = rejected
        worker._maybe_heartbeat()
        self.assertEqual(read_uplink(self.path, self.scope), "auth error")
        worker.erp.heartbeat = lambda **kw: {"urgent": [
            {"command_id": "urgent", "command_type": "command", "payload": {"cmd": "next"}}
        ]}
        worker._maybe_heartbeat()
        self.assertEqual(read_uplink(self.path, self.scope), "online")
        self.assertTrue(self.q.has_command("urgent"))

    def test_console_uses_worker_state_not_log_lines(self):
        from gateway.config import Settings
        from console.data import GatewayData
        data = GatewayData(Settings(_env_file=None, erp_base_url=self.s.erp_base_url,
                                    gateway_key=self.s.gateway_key, gateway_id=self.s.gateway_id,
                                    db_path=self.path))
        data.rb = SimpleNamespace(playback_info=lambda: None, playlist=lambda: [])
        Path(data.log_path()).write_text(
            "2026-10-28 10:00:00,000 WARNING lease failed (uplink down?)\n"
        )
        record_heartbeat(self.q.db, self.scope)
        self.assertEqual(data.snapshot()["uplink"], "online")
        record_heartbeat(self.q.db, self.scope, RuntimeError("network down"))
        self.assertEqual(data.snapshot()["uplink"], "offline")


if __name__ == "__main__":
    unittest.main(verbosity=2)
