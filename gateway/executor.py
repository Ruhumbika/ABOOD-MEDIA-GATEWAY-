"""Apply one leased command to RadioBOSS and produce an ack result.

For inserttrack this enforces the *confirm properly* rule locally — send, then
verify the playlist grew by one for this creative (a count delta, so a creative
that airs more than once confirms each insert independently). A re-attempt of
the SAME command (its prior try crashed before it was recorded) is confirmed
without re-inserting, so it never double-airs. The result's `confirmed` flag is
what the server uses to decide whether the spot becomes SCHEDULED.

The creative file is staged onto the studio disk by the worker BEFORE we get
here (see FileSync.stage); this function never blocks on a download — it expects
payload["file"] to already be a local path.
"""

from __future__ import annotations

import logging
import time

from .radioboss import RadioBoss

log = logging.getLogger("gateway.executor")


def _result(command_id: str, ok: bool, confirmed: bool | None, t0: float, response: str | None) -> dict:
    return {
        "command_id": command_id,
        "ok": ok,
        "confirmed": confirmed,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "response": (response or "")[:2000] or None,
    }


def _force_play(rb: RadioBoss, cid: str, payload: dict, t0: float) -> dict:
    """Force a must-air creative onto air over whatever is currently playing.

    Insert it at the top of the queue, cue it as the next track, then honour the
    cut mode:
      HARD       — skip the current track now so the creative airs immediately.
      SAFE_BREAK — leave it cued; the current track finishes at its natural
                   boundary and the creative airs next (never clips a live read
                   or a paid spot mid-sentence).

    The creative file is staged locally by the worker before we get here, same as
    inserttrack — payload["file"] is a local path.
    """
    file = payload.get("file")
    if not file:
        return _result(cid, False, False, t0, "force_play has no local file")

    mode = str(payload.get("mode") or "HARD").upper()

    # 1) Insert at the very top of the queue.
    ok, body = rb.execute("inserttrack", {
        "file": file,
        "title": payload.get("title"),
        "pos": payload.get("pos") or 1,
    })
    if not ok:
        return _result(cid, False, False, t0, f"force_play insert failed: {body}")

    # 2) Cue the freshly-inserted creative as the next track (find where it
    #    actually landed rather than trusting a fixed index).
    pos = rb.first_position({"file": file, "title": payload.get("title")})
    if pos:
        rb.execute("setnexttrack", {"position": pos})

    # 3) HARD cuts to it now; SAFE_BREAK lets the current item finish first.
    if mode == "HARD":
        ok2, body2 = rb.execute("command", {"cmd": "next"})
        return _result(cid, ok2, ok2, t0, f"force HARD (pos={pos}): {body2 or body}")

    return _result(cid, True, True, t0, f"force SAFE_BREAK (pos={pos}): cued next, current finishes")


def execute_op(rb: RadioBoss, op: dict) -> dict:
    cid = op["command_id"]
    ctype = op["command_type"]
    payload = op.get("payload") or {}
    t0 = time.monotonic()

    if ctype == "force_play":
        return _force_play(rb, cid, payload, t0)

    if ctype == "inserttrack":
        # The worker stages the local file first; a managed creative with no
        # local path means staging isn't done — don't guess a server path.
        if payload.get("asset_id") and not payload.get("file"):
            return _result(cid, False, False, t0, "creative file not staged locally")

        attempts = int(op.get("attempts") or 0)

        # Re-attempt idempotency: only when THIS command was tried before (a
        # crash between the insert landing and it being recorded). A fresh
        # repeated spot is a different command_id with attempts == 0, so it is
        # NOT short-circuited here and airs as it should.
        if attempts > 0 and rb.in_playlist(payload):
            log.info("inserttrack %s already present on re-attempt — confirmed, not re-inserted", cid[:8])
            return _result(cid, True, True, t0, "already present in playlist (re-attempt)")

        before = rb.count_in_playlist(payload)
        ok, body = rb.execute("inserttrack", payload)
        if not ok:
            return _result(cid, False, False, t0, body)

        # Confirm by count delta — the playlist must have grown by one for this
        # creative. Robust when the same creative appears multiple times.
        after = rb.count_in_playlist(payload)
        confirmed = after > before
        if not confirmed:
            log.warning("inserttrack %s accepted but playlist count did not rise (%d→%d)",
                        cid[:8], before, after)
        return _result(cid, True, confirmed, t0, body)

    # Other write commands: an OK response is sufficient.
    ok, body = rb.execute(ctype, payload)
    return _result(cid, ok, ok, t0, body)
