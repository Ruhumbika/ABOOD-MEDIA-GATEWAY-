"""Run: python tests/test_proof_of_play.py (no RadioBOSS/ERP required)."""
from pathlib import Path
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.config import Settings
from gateway.erp_client import ErpClient, ErpError
from gateway.proof_of_play import PlayOutbox, event_scope, read_play_events
from gateway.queue import LocalQueue
from gateway.radioboss import RadioBoss
from gateway.worker import Worker


def track(start="2026-10-30 10:00:00", filename=r"C:\Ads\Spot.mp3"):
    return dict(start_time=start, filename=filename, title="Spot", artist="ABOOD", duration="00:30")


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "gateway.db")
        self.q = LocalQueue(self.path)
        self.box = PlayOutbox(self.q.db, "scope", "studio-01")
        self.erp = Mock()

    def tearDown(self):
        self.q.close()
        self.tmp.cleanup()

    def events(self):
        return read_play_events(self.path, "scope")

    def test_repeat_history_and_restart_deduplicate(self):
        self.assertEqual(self.box.capture([track()], now=10), (1, 0))
        self.assertEqual(self.box.capture([track(filename=r"c:/ads/spot.mp3")], now=11), (0, 0))
        self.q.close()
        self.q = LocalQueue(self.path)
        self.box = PlayOutbox(self.q.db, "scope", "studio-01")
        self.assertEqual(self.box.capture([track()], now=12), (0, 0))
        self.assertEqual(self.events()["pending"], 1)

    def test_same_file_different_start_is_separate_play(self):
        self.box.capture([track(), track("2026-10-30 11:00:00")])
        self.assertEqual(self.events()["pending"], 2)

    def test_missing_timestamp_not_inferred_from_duration_or_observation(self):
        self.assertEqual(self.box.capture([track(""), track("invalid"), track(filename="")]), (0, 3))
        self.assertEqual(self.events()["pending"], 0)

    def test_empty_history_adds_nothing(self):
        self.assertEqual(self.box.capture([]), (0, 0))

    def test_source_time_preserved_without_claiming_utc_or_completion(self):
        self.box.capture([track()], now=0)
        event = self.events()["events"][0]
        self.assertEqual(event["started_at_local"], "2026-10-30 10:00:00")
        self.assertIsNone(event["source_timezone"])
        self.assertEqual(event["evidence"], "playout_start_observed")
        self.assertEqual(event["observed_at"], "1970-01-01T00:00:00+00:00")
        self.assertNotIn("completed", event)

    def test_delivery_disabled_does_not_make_request(self):
        self.box.capture([track()])
        self.assertEqual(self.box.flush(self.erp, ""), 0)
        self.erp.report_play_events.assert_not_called()

    def test_outage_retry_has_backoff_same_event_id_and_recovers(self):
        self.box.capture([track()], now=0)
        self.erp.report_play_events.side_effect = ErpError("secret-key secret-body")
        self.assertEqual(self.box.flush(self.erp, "gateway/play-events", now=10), 0)
        event = self.events()["events"][0]
        self.assertEqual(event["attempts"], 1)
        self.assertNotIn("secret", event["last_error"])
        self.box.flush(self.erp, "gateway/play-events", now=14)
        self.assertEqual(self.erp.report_play_events.call_count, 1)
        self.erp.report_play_events.side_effect = None
        self.erp.report_play_events.return_value = [event["event_id"]]
        self.assertEqual(self.box.flush(self.erp, "gateway/play-events", now=15), 1)
        self.assertEqual(self.events()["pending"], 0)
        self.assertEqual(self.events()["sent"], 1)
        self.assertEqual(self.box.capture([track()], now=16), (0, 0))
        self.box.flush(self.erp, "gateway/play-events", now=100)
        self.assertEqual(self.erp.report_play_events.call_count, 2)

    def test_partial_ack_keeps_unacknowledged_and_ignores_unknown(self):
        self.box.capture([track(), track("2026-10-30 11:00:00")], now=0)
        event_id = self.events()["events"][0]["event_id"]
        self.erp.report_play_events.return_value = [event_id, "unknown-id"]
        self.assertEqual(self.box.flush(self.erp, "gateway/play-events", now=10), 1)
        self.assertEqual((self.events()["pending"], self.events()["sent"]), (1, 1))

    def test_crash_after_remote_acceptance_retries_same_id(self):
        self.box.capture([track()], now=0)
        calls = []
        def lost_response(path, events):
            calls.append(events[0]["event_id"])
            raise ErpError("response lost after server commit")
        self.erp.report_play_events.side_effect = lost_response
        self.box.flush(self.erp, "gateway/play-events", now=1)
        def duplicate_ack(path, events):
            calls.append(events[0]["event_id"])
            return [events[0]["event_id"]]
        self.erp.report_play_events.side_effect = duplicate_ack
        self.box.flush(self.erp, "gateway/play-events", now=6)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(self.events()["sent"], 1)

    def test_other_station_scope_never_delivers_old_events(self):
        self.box.capture([track()], now=0)
        other = PlayOutbox(self.q.db, "other", "studio-02")
        self.assertEqual(other.flush(self.erp, "gateway/play-events", now=10), 0)
        self.erp.report_play_events.assert_not_called()
        self.assertEqual(self.events()["pending"], 1)

    def test_key_rotation_keeps_scope_destination_change_does_not(self):
        s = Settings(erp_base_url="https://erp.test/api/v1", gateway_key="first")
        original = event_scope(s)
        s.gateway_key = "rotated"
        self.assertEqual(event_scope(s), original)
        s.gateway_id = "other"
        self.assertNotEqual(event_scope(s), original)
        s.gateway_id = "gw-1"
        s.erp_base_url = "https://other.test/api/v1"
        self.assertNotEqual(event_scope(s), original)

    def test_work_and_command_acks_untouched(self):
        before = self.q.depth()
        self.box.capture([track()], now=0)
        self.erp.report_play_events.return_value = []
        self.box.flush(self.erp, "gateway/play-events", now=10)
        self.assertEqual(self.q.depth(), before)

    def test_read_only_missing_and_old_database(self):
        missing = Path(self.tmp.name) / "missing.db"
        self.assertFalse(read_play_events(str(missing), "scope")["available"])
        self.assertFalse(missing.exists())
        old = Path(self.tmp.name) / "old.db"
        with sqlite3.connect(old) as db:
            db.execute("CREATE TABLE old_schema (id INTEGER)")
        self.assertFalse(read_play_events(str(old), "scope")["available"])
        with sqlite3.connect(old) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='play_events'").fetchone())

    def test_batch_is_bounded(self):
        self.box.capture([track(f"2026-10-30 10:{i // 60:02d}:{i % 60:02d}") for i in range(70)], now=0)
        self.erp.report_play_events.return_value = []
        self.box.flush(self.erp, "gateway/play-events", now=10)
        self.assertEqual(len(self.erp.report_play_events.call_args.args[1]), 50)

    def test_worker_captures_only_history_not_playlist(self):
        worker = Worker.__new__(Worker)
        worker.proof = self.box
        worker.rb = Mock()
        worker.rb.play_history.return_value = [track()]
        worker._capture_play_history()
        self.assertEqual(self.events()["pending"], 1)
        worker.rb.playlist.assert_not_called()
        worker.rb.execute.assert_not_called()
        worker.rb.play_history.return_value = None
        worker._capture_play_history()
        self.assertEqual(self.events()["pending"], 1)

    def test_history_store_failure_does_not_block_commands(self):
        worker = Worker.__new__(Worker)
        worker.proof = None
        worker._capture_play_history = Mock(side_effect=sqlite3.OperationalError("disk full"))
        for name in ["_lease", "_execute_pending", "_flush_acks", "_maybe_verify",
                     "_maybe_heartbeat", "_maybe_publish", "_governance", "_poll_sms", "_flush_interactions"]:
            setattr(worker, name, Mock())
        worker.tick()
        worker._lease.assert_called_once()
        worker._execute_pending.assert_called_once()

    def test_disabled_worker_does_not_read_history(self):
        worker = Worker.__new__(Worker)
        worker.proof = None
        worker.rb = Mock()
        worker._capture_play_history()
        worker.rb.play_history.assert_not_called()


class RadioBossTests(unittest.TestCase):
    def test_official_history_shape_preserves_start_time(self):
        rb = RadioBoss("http://localhost", "")
        rb._call = Mock(return_value='''<LastPlayed><TRACK TITLE="Spot" FILENAME="C:/Spot.mp3"
            STARTTIME="2026-10-30 10:00:00" LASTPLAYED="2026-10-30 11:00:00" DURATION="00:30"/></LastPlayed>''')
        history = rb.play_history()
        self.assertEqual(history[0]["start_time"], "2026-10-30 10:00:00")
        rb._call.assert_called_once_with({"action": "getlastplayed"})

    def test_failed_or_wrong_response_is_not_empty_success(self):
        rb = RadioBoss("http://localhost", "")
        for body in [None, "bad XML", "<Info/>"]:
            rb._call = Mock(return_value=body)
            self.assertIsNone(rb.play_history())
        rb._call = Mock(return_value="<LastPlayed/>")
        self.assertEqual(rb.play_history(), [])


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.client = ErpClient("https://erp.test/api/v1", "secret", "studio-01")
        self.client.session = Mock()
        self.response = self.client.session.post.return_value
        self.response.status_code = 200

    def test_explicit_ack_request_contract(self):
        self.response.json.return_value = {"accepted_event_ids": ["id1"]}
        self.assertEqual(self.client.report_play_events("gateway/play-events", [{"event_id": "id1"}]), ["id1"])
        call = self.client.session.post.call_args
        self.assertEqual(call.args[0], "https://erp.test/api/v1/gateway/play-events")
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["json"]["schema_version"], 1)
        self.assertEqual(call.kwargs["json"]["gateway_id"], "studio-01")

    def test_generic_200_is_not_an_ack(self):
        for body in [{"ok": True}, None, [], {"accepted_event_ids": "id1"}, {"accepted_event_ids": [1]}]:
            self.response.json.return_value = body
            with self.assertRaises(ErpError):
                self.client.report_play_events("gateway/play-events", [])

    def test_rejects_nonrelative_paths_before_sending_key(self):
        for path in ["https://evil.test", "//evil.test", "/gateway/play-events", "../other", "gateway/%2e%2e/x", "gateway/x?key=foo", "gateway\\x", "gateway//x", "gateway/x#fragment", "http://["]:
            with self.assertRaises(ErpError, msg=path):
                self.client.report_play_events(path, [])
        self.client.session.post.assert_not_called()

    def test_http_redirect_auth_and_server_errors_keep_pending(self):
        for status in [301, 401, 403, 404, 500]:
            self.response.status_code = status
            with self.assertRaises(ErpError):
                self.client.report_play_events("gateway/play-events", [])


class HttpIntegrationTests(unittest.TestCase):
    def test_real_http_outage_recovery_and_duplicate_ack(self):
        received = []
        accepted = set()
        fail = [True]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.server.test_case.assertEqual(self.path, "/api/v1/gateway/play-events")
                self.server.test_case.assertEqual(self.headers.get("X-Gateway-Key"), "local-test-key")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(body)
                if fail[0]:
                    status, response = 503, {"error": "temporary outage"}
                else:
                    ids = [e["event_id"] for e in body["events"]]
                    accepted.update(ids)  # backend must ACK duplicates as well
                    status, response = 200, {"accepted_event_ids": ids}
                data = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.test_case = self
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ErpClient(f"http://127.0.0.1:{server.server_port}/api/v1", "local-test-key", "studio-01")
            with tempfile.TemporaryDirectory() as tmp:
                q = LocalQueue(str(Path(tmp) / "gateway.db"))
                try:
                    box = PlayOutbox(q.db, "scope", "studio-01")
                    box.capture([track()], now=0)
                    self.assertEqual(box.flush(client, "gateway/play-events", now=1), 0)
                    fail[0] = False
                    self.assertEqual(box.flush(client, "gateway/play-events", now=6), 1)
                    ids = client.report_play_events("gateway/play-events", received[-1]["events"])
                    self.assertEqual(set(ids), accepted)
                    self.assertEqual(len(accepted), 1)
                    state = read_play_events(str(Path(tmp) / "gateway.db"), "scope")
                    self.assertEqual((state["pending"], state["sent"]), (0, 1))
                finally:
                    q.close()
                client.session.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
