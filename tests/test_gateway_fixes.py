"""Regression tests for gateway hardening fixes #1–#5.

Run:  py tests/test_gateway_fixes.py     (from the project root)

No pytest required — a tiny runner at the bottom executes every test_* and
reports. Uses in-process fakes for RadioBOSS / MediaERP; touches no network.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.executor import execute_op
from gateway.queue import LocalQueue
from gateway.manifest import ApprovedManifest
from gateway.classifier import ContentClassifier
from gateway.filesync import FileSync, READY, PENDING, FAILED
import hashlib


# ── fakes ───────────────────────────────────────────────────────────────
class FakeRB:
    """A RadioBOSS stand-in with an in-memory playlist."""
    def __init__(self, playlist=None):
        self.playlist_data = list(playlist or [])
        self.insert_calls = 0
        self.deleted = None
        self.commands = []
        self.current = {}
        self.next_set = None       # last setnexttrack position
        self.cast_titles = []      # setcasttitle history

    def execute(self, ctype, args):
        if ctype == "inserttrack":
            self.insert_calls += 1
            self.playlist_data.append(
                {"filename": args.get("file", ""), "casttitle": args.get("title", ""),
                 "position": len(self.playlist_data) + 1}
            )
            return (True, "OK")
        if ctype == "delete":
            self.deleted = list(args.get("positions") or [])
            return (True, "OK")
        if ctype == "command":
            self.commands.append(args.get("cmd"))
            return (True, "OK")
        if ctype == "setnexttrack":
            self.next_set = args.get("position")
            return (True, "OK")
        if ctype == "setcasttitle":
            self.cast_titles.append(args.get("title"))
            return (True, "OK")
        return (True, "OK")

    def first_position(self, args):
        f = (args.get("file") or "").lower()
        t = (args.get("title") or "").lower()
        for tr in self.playlist_data:
            if (f and tr.get("filename", "").lower() == f) or (t and tr.get("casttitle", "").lower() == t):
                return tr.get("position")
        return None

    def count_in_playlist(self, args):
        f = (args.get("file") or "").lower()
        t = (args.get("title") or "").lower()
        n = 0
        for tr in self.playlist_data:
            if (f and tr.get("filename", "").lower() == f) or (t and tr.get("casttitle", "").lower() == t):
                n += 1
        return n

    def in_playlist(self, args):
        return self.count_in_playlist(args) > 0

    def playback_info(self):
        return {"current": self.current}


def _op(cid, file="a.mp3", title="Ad A", attempts=0, **payload_extra):
    payload = {"file": file, "title": title, **payload_extra}
    return {"command_id": cid, "command_type": "inserttrack", "attempts": attempts, "payload": payload}


# ── #1 confirm correctness ──────────────────────────────────────────────
def test_repeated_spot_both_air_and_confirm():
    """Same creative, two DIFFERENT commands (a repeat booking) → both insert,
    both confirm via count delta. The old presence check confirmed the 2nd
    without inserting it (silent under-delivery)."""
    rb = FakeRB()
    r1 = execute_op(rb, _op("c1"))
    r2 = execute_op(rb, _op("c2"))
    assert r1["ok"] and r1["confirmed"], r1
    assert r2["ok"] and r2["confirmed"], r2
    assert rb.insert_calls == 2, "both spots must be inserted"
    assert len(rb.playlist_data) == 2


def test_reattempt_does_not_double_insert():
    """A re-attempt of the SAME command (attempts>0) that already landed is
    confirmed WITHOUT re-inserting → no double-air."""
    rb = FakeRB(playlist=[{"filename": "a.mp3", "casttitle": "Ad A"}])
    r = execute_op(rb, _op("c1", attempts=1))
    assert r["ok"] and r["confirmed"], r
    assert rb.insert_calls == 0, "must not re-insert on a re-attempt"


def test_fresh_spot_inserts_even_if_same_creative_present():
    """attempts==0 (a fresh command) still inserts even when the creative is
    already in the playlist from another spot."""
    rb = FakeRB(playlist=[{"filename": "a.mp3", "casttitle": "Ad A"}])
    r = execute_op(rb, _op("c9", attempts=0))
    assert rb.insert_calls == 1
    assert r["confirmed"], "count 1→2 confirms this insert"


def test_unstaged_managed_creative_is_not_aired():
    rb = FakeRB()
    op = {"command_id": "c", "command_type": "inserttrack", "attempts": 0,
          "payload": {"asset_id": 5, "title": "x"}}  # asset but no local file
    r = execute_op(rb, op)
    assert not r["ok"] and "not staged" in (r["response"] or "")
    assert rb.insert_calls == 0


# ── #1 queue-level idempotency ──────────────────────────────────────────
def test_queue_dedup_against_acks():
    with tempfile.TemporaryDirectory() as d:
        q = LocalQueue(os.path.join(d, "q.db"))
        op = {"command_id": "x1", "command_type": "command", "payload": {"cmd": "next"}}
        assert q.add_work([op]) == 1
        assert q.add_work([op]) == 0, "PK dedup while still in work"
        q.mark_executed("x1", {"ok": True, "confirmed": True, "duration_ms": 1, "response": "OK"})
        # Re-leased while its ack is still buffered → must NOT re-queue (double-air).
        assert q.add_work([op]) == 0, "must not re-queue a command awaiting ack"
        q.close()


def test_queue_bump_attempt():
    with tempfile.TemporaryDirectory() as d:
        q = LocalQueue(os.path.join(d, "q.db"))
        q.add_work([{"command_id": "x", "command_type": "command", "payload": {}}])
        q.bump_attempt("x")
        q.bump_attempt("x")
        assert q.pending_work()[0]["attempts"] == 2
        q.close()


# ── #3 manifest basename normalization ──────────────────────────────────
class _ManErp:
    def __init__(self, data):
        self._data = data
    def get_manifest(self):
        return self._data


def test_manifest_keys_by_basename_and_classifier_matches():
    data = {"assets": [{"filename": "ads/2026/spot1.mp3", "type": "AD", "checksum": "deadbeef"}],
            "policy": {}}
    m = ApprovedManifest(_ManErp(data))
    assert m.sync()
    assert "spot1.mp3" in m.by_filename, m.by_filename  # NOT 'ads/2026/spot1.mp3'
    c = ContentClassifier(m)
    # RadioBOSS reports a full local path; basename must still match → approved.
    assert c.classify({"filename": r"C:\studio\media\spot1.mp3"})["approved"] is True
    assert c.classify({"filename": "unknown.mp3"})["approved"] is False


# ── #5 non-blocking file staging ────────────────────────────────────────
class _DlErp:
    def __init__(self, payload=b"hello", fail=False, delay=0.0):
        self.payload = payload
        self.fail = fail
        self.delay = delay
        self.calls = 0
    def download_file_to(self, asset_id, dest_path):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        with open(dest_path, "wb") as fh:
            fh.write(self.payload)
        return hashlib.sha256(self.payload).hexdigest(), None


def _wait_stage(fs, item, want, timeout=5.0):
    end = time.monotonic() + timeout
    st, local, reason = fs.stage(item)
    while st not in want and time.monotonic() < end:
        time.sleep(0.02)
        st, local, reason = fs.stage(item)
    return st, local, reason


def test_stage_downloads_in_background_then_ready():
    with tempfile.TemporaryDirectory() as d:
        erp = _DlErp(payload=b"creative-bytes", delay=0.1)
        fs = FileSync(erp, os.path.join(d, "media"), download_workers=1)
        item = {"asset_id": 1, "filename": "x.mp3",
                "checksum": hashlib.sha256(b"creative-bytes").hexdigest()}
        first, _, _ = fs.stage(item)
        assert first == PENDING, "must return immediately, not block on download"
        st, local, _ = _wait_stage(fs, item, {READY, FAILED})
        assert st == READY and local and os.path.isfile(local)
        calls_after_ready = erp.calls
        # Already staged → ready with no new download.
        assert fs.stage(item)[0] == READY
        assert erp.calls == calls_after_ready, "no redundant re-download"
        fs.close()


def test_stage_surfaces_download_failure_once():
    with tempfile.TemporaryDirectory() as d:
        erp = _DlErp(fail=True)
        fs = FileSync(erp, os.path.join(d, "media"), download_workers=1)
        item = {"asset_id": 2, "filename": "y.mp3"}
        st, _, reason = _wait_stage(fs, item, {FAILED})
        assert st == FAILED and "boom" in (reason or "")
        fs.close()


def test_integrity_mismatch_rejected():
    with tempfile.TemporaryDirectory() as d:
        erp = _DlErp(payload=b"real-bytes")
        fs = FileSync(erp, os.path.join(d, "media"), download_workers=1)
        item = {"asset_id": 3, "filename": "z.mp3", "checksum": "not-the-right-hash"}
        st, _, _ = _wait_stage(fs, item, {FAILED})
        assert st == FAILED, "a checksum mismatch must not be served"
        fs.close()


# ── #2 + #4 worker behaviour (needs Settings) ───────────────────────────
def _make_worker(tmp, **over):
    from gateway.config import Settings
    from gateway.worker import Worker
    s = Settings(_env_file=None, erp_base_url="http://x/api/v1", gateway_key="k",
                 db_path=os.path.join(tmp, "gw.db"),
                 media_folder=os.path.join(tmp, "media"),
                 schedule_folder=os.path.join(tmp, "sched"),
                 max_execute_attempts=over.get("max_execute_attempts", 3))
    return Worker(s)


def test_poison_op_is_deadlettered_not_stuck():
    """#4 — an op whose execution keeps raising must dead-letter after N tries,
    not spin forever blocking the queue."""
    import gateway.worker as W
    with tempfile.TemporaryDirectory() as tmp:
        w = _make_worker(tmp, max_execute_attempts=3)
        w.rb = FakeRB()
        w.q.add_work([{"command_id": "p1", "command_type": "command", "payload": {"cmd": "next"}}])
        orig = W.execute_op
        W.execute_op = lambda rb, op: (_ for _ in ()).throw(RuntimeError("poison"))
        try:
            for _ in range(3):
                w._execute_pending()
        finally:
            W.execute_op = orig
        assert not any(o["command_id"] == "p1" for o in w.q.pending_work()), "must not stay stuck in work"
        acks = w.q.pending_acks()
        assert any(a["command_id"] == "p1" and a["ok"] is False for a in acks), "must be dead-lettered as failed"
        w.filesync.close(); w.q.close()


class _NoPoll:
    def poll(self):
        return []


class _RecStub:
    def __init__(self, v):
        self.v = v
    def check(self):
        return list(self.v)


class _ErpStub:
    def __init__(self):
        self.reported = []
    def report_violations(self, payload):
        self.reported.append(payload)
        return {}


def _governance_with(w, current):
    w.manifest.loaded = True
    w.manifest.policy = {"auto_cut": True}
    w.manifest.sync = lambda: False        # don't re-sync/reconfigure in the test
    w.usb = _NoPoll(); w.process = _NoPoll()
    w.erp = _ErpStub()
    rb = FakeRB()
    rb.current = current
    w.rb = rb
    viol = [{"type": "UNAPPROVED_CONTENT", "severity": "error", "summary": "bad",
             "detail": {"filename": "bad.mp3", "title": "Bad", "casttitle": "Bad"},
             "position": 5}]
    w.reconciler = _RecStub(viol)
    w._governance()
    return rb


def test_autocut_does_not_skip_when_onair_track_is_approved():
    """#2 — unapproved item is QUEUED (pos 5) but a legit track is on air.
    Delete it, but do NOT send 'next' (which would cut the on-air track)."""
    with tempfile.TemporaryDirectory() as tmp:
        w = _make_worker(tmp)
        rb = _governance_with(w, current={"filename": "good.mp3", "casttitle": "Good"})
        assert rb.deleted == [5], "the unapproved queued item must be deleted"
        assert "next" not in rb.commands, "must NOT skip an approved on-air track"
        w.filesync.close(); w.q.close()


def test_autocut_skips_when_onair_track_is_unapproved():
    """#2 — when the on-air track itself is unapproved, delete AND 'next'."""
    with tempfile.TemporaryDirectory() as tmp:
        w = _make_worker(tmp)
        rb = _governance_with(w, current={"filename": "bad.mp3", "casttitle": "Bad"})
        assert rb.deleted == [5]
        assert "next" in rb.commands, "an unapproved on-air track must be skipped"
        w.filesync.close(); w.q.close()


# ── Phase B: must-air force_play + presenter cue ────────────────────────
def _force_op(cid, mode="HARD", file="emergency.mp3", title="Emergency"):
    return {"command_id": cid, "command_type": "force_play", "attempts": 0,
            "payload": {"file": file, "title": title, "mode": mode, "pos": 1}}


def test_force_play_hard_inserts_and_cuts():
    """HARD force_play inserts the creative, cues it as next, and skips the
    current track ('next') so it airs immediately."""
    rb = FakeRB()
    r = execute_op(rb, _force_op("f1", mode="HARD"))
    assert r["ok"] and r["confirmed"], r
    assert rb.insert_calls == 1, "must insert the must-air creative"
    assert rb.next_set == 1, "must cue the inserted creative as next"
    assert "next" in rb.commands, "HARD must cut the current track"


def test_force_play_safe_break_waits_for_current():
    """SAFE_BREAK inserts and cues the creative but does NOT skip the current
    track — it airs at the next natural boundary, never mid-item."""
    rb = FakeRB()
    r = execute_op(rb, _force_op("f2", mode="SAFE_BREAK"))
    assert r["ok"] and r["confirmed"], r
    assert rb.insert_calls == 1
    assert rb.next_set == 1, "must cue it as next"
    assert "next" not in rb.commands, "SAFE_BREAK must NOT interrupt the current track"


def test_force_play_without_file_fails():
    rb = FakeRB()
    r = execute_op(rb, {"command_id": "f3", "command_type": "force_play",
                        "attempts": 0, "payload": {"mode": "HARD"}})
    assert not r["ok"] and "no local file" in (r["response"] or "")
    assert rb.insert_calls == 0


def test_presenter_cue_delivered_and_acked():
    """A presenter_cue is stored for the console overlay AND pushed to the
    RadioBOSS cast title, then acked ok — never sent as a track to insert."""
    with tempfile.TemporaryDirectory() as tmp:
        w = _make_worker(tmp)
        w.rb = FakeRB()
        w.q.add_work([{
            "command_id": "cue1", "command_type": "presenter_cue", "priority": 10,
            # Far-future air time so the cue's display window is always open
            # (a past air_at would be pruned as expired — correct, but flaky here).
            "payload": {"asset_id": 7, "title": "Read the weather", "script": "Sunny, 30C",
                        "air_at": "2099-01-01T00:00:00+00:00"},
        }])
        w._execute_pending()

        # Not inserted as a track.
        assert w.rb.insert_calls == 0
        # Surface 2 — cast title carries the cue.
        assert any("Read the weather" in (t or "") for t in w.rb.cast_titles), w.rb.cast_titles
        # Surface 1 — stored for the console overlay.
        row = w.q.db.execute("SELECT title, script FROM presenter_cues WHERE command_id='cue1'").fetchone()
        assert row is not None and row["script"] == "Sunny, 30C", row
        # Delivery acked ok.
        assert any(a["command_id"] == "cue1" and a["ok"] for a in w.q.pending_acks())
        w.filesync.close(); w.q.close()


# ── runner ──────────────────────────────────────────────────────────────
def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as exc:
            import traceback
            print(f"  FAIL  {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed ({len(tests)} total)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
