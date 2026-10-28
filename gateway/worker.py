"""The gateway worker loop.

Each tick, in an order chosen for offline resilience:
  1. lease new work from MediaERP        (skipped silently if the uplink is down)
  2. execute pending work on RadioBOSS    (runs from the local queue regardless)
  3. flush buffered acks back to MediaERP  (retried until the uplink returns)
  4. heartbeat RadioBOSS health + live     (on its own cadence)

Because work and acks are persisted locally, a dropped uplink never stops
playout — the studio keeps airing and syncs up when the link is back.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date as _date
from datetime import datetime, timedelta, timezone

from . import __version__
from .ai import AiClient
from .audio import AudioMonitor
from .classifier import ContentClassifier
from .config import Settings
from .erp_client import ErpClient, ErpError
from .executor import execute_op
from .filesync import FAILED, PENDING, FileSync
from .governance.folder_policy import FolderPolicy
from .governance.process_monitor import ProcessMonitor
from .governance.usb_monitor import UsbMonitor
from .manifest import ApprovedManifest
from .publisher import SchedulePublisher
from .fingerprint import Fingerprinter, ReferenceIndex
from .queue import LocalQueue
from .uplink import heartbeat_scope, record_heartbeat
from .radioboss import RadioBoss
from .recorder import ComplianceRecorder
from .transcriber import Transcriber
from .reconciler import PlaylistReconciler
from .sms import make_sms_reader

log = logging.getLogger("gateway.worker")

try:  # optional — telemetry degrades to buffer/clock only if psutil is absent
    import psutil  # type: ignore
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.erp = ErpClient(settings.erp_base_url, settings.gateway_key, settings.gateway_id, settings.http_timeout)
        self.rb = RadioBoss(
            settings.radioboss_base_url,
            settings.radioboss_password,
            settings.radioboss_user,
            settings.radioboss_timeout,
            settings.insert_position,
        )
        self.q = LocalQueue(settings.db_path)
        self.filesync = FileSync(self.erp, settings.media_folder, settings.download_workers)
        self.publisher = SchedulePublisher(self.erp, self.filesync, settings.schedule_folder)

        # Governance (Phase 6)
        self.manifest = ApprovedManifest(self.erp)
        self.classifier = ContentClassifier(self.manifest)
        self.reconciler = PlaylistReconciler(self.rb, self.classifier)
        self.usb = UsbMonitor({})       # policy filled in after first manifest sync
        self.process = ProcessMonitor({})
        self.folders = FolderPolicy({})

        self._stop = False
        self._last_heartbeat = 0.0
        self._last_manifest = 0.0
        self._published_date: str | None = None
        self._started = time.monotonic()
        self.audio = AudioMonitor(settings)   # Tier C — on-air metering + dead-air
        self.recorder = ComplianceRecorder(settings)  # Tier C — as-aired MP3 logger
        self.transcriber = Transcriber(settings)      # Tier C — Whisper transcription
        self._last_dead_air = 0.0
        # Tier C (C3) — fingerprinting / ACR (experimental, opt-in)
        self.fingerprinter: Fingerprinter | None = None
        self.refindex: ReferenceIndex | None = None
        if getattr(settings, "fingerprint_enabled", False):
            self.fingerprinter = Fingerprinter(sr=settings.fp_sr)
            self.refindex = ReferenceIndex(settings.fp_db_path, self.fingerprinter, settings.ffmpeg_path or "ffmpeg")
        self._verification: dict | None = None
        self._last_verify = 0.0
        self._last_mismatch = 0.0
        self.ai = AiClient(settings)          # optional local LLM (Ollama)
        self._ai_cache: dict[str, dict] = {}  # external_id → enrichment (survives uplink retries)
        self._ad_read_done: set = set()       # (asset_id, filename) live-reads already verified
        if psutil is not None:
            try:
                psutil.cpu_percent(interval=None)  # prime — first reading is meaningless
            except Exception:  # noqa: BLE001
                pass

        # Listener SMS ingest (studio phone via ADB, or a GSM modem) — optional.
        self.sms = make_sms_reader(settings)
        self._last_sms_poll = 0.0

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        log.info("Studio Gateway starting — erp=%s id=%s radioboss=%s",
                 self.s.erp_base_url, self.s.gateway_id, self.s.radioboss_base_url)
        self.audio.start()
        self.recorder.start()
        self.transcriber.start()
        while not self._stop:
            try:
                self.tick()
            except Exception as exc:  # never let a bad tick kill the daemon
                log.exception("tick failed: %s", exc)
            self._sleep(self.s.poll_interval)
        self.transcriber.stop()
        self.recorder.stop()
        self.audio.stop()
        self.filesync.close()
        if self.sms:
            self.sms.close()
        self.q.close()
        log.info("Studio Gateway stopped")

    def tick(self) -> None:
        self._lease()
        self._execute_pending()
        self._flush_acks()
        self._maybe_verify()      # Tier C — fingerprint the on-air sound vs schedule
        self._maybe_heartbeat()   # also receives urgent cut/switch commands
        self._maybe_publish()
        self._governance()
        self._poll_sms()          # read inbound listener SMS (studio-local)
        self._flush_interactions()  # register them as reach (retried until uplink up)

    # ── steps ──────────────────────────────────────────────────────────
    def _lease(self) -> None:
        try:
            ops = self.erp.lease(self.s.lease_limit)
        except ErpError as exc:
            log.warning("lease failed (uplink down?): %s", exc)
            return
        if ops:
            added = self.q.add_work(ops)
            log.info("leased %d op(s) (%d new)", len(ops), added)

    def _execute_pending(self) -> None:
        for op in self.q.pending_work():
            cid = op["command_id"]
            payload = op.get("payload") or {}

            # Phase B: a presenter cue is delivered to the studio (console overlay
            # + RadioBOSS cast title), not sent to the automation as a track.
            if op["command_type"] == "presenter_cue":
                result = self._deliver_cue(op)
                self.q.mark_executed(cid, result)
                log.info("delivered presenter cue %s ok=%s", cid[:8], result["ok"])
                continue

            # Phase 3: stage the creative locally WITHOUT blocking on a download.
            # A pending download leaves the op queued (no attempt burned) so the
            # rest of the batch — and urgent commands — aren't stalled behind it.
            # force_play needs the same local staging as inserttrack.
            if op["command_type"] in ("inserttrack", "force_play") and payload.get("asset_id"):
                status, local, reason = self.filesync.stage(payload)
                if status == PENDING:
                    continue
                if status == FAILED:
                    self._fail_attempt(op, f"creative not available: {reason}", pre_execute=True)
                    continue
                payload = {**payload, "file": local}
                op = {**op, "payload": payload}

            # Persist the attempt BEFORE the RadioBOSS call so a crash mid-execute
            # is visible on restart (feeds dead-lettering + re-attempt idempotency).
            self.q.bump_attempt(cid)
            try:
                result = execute_op(self.rb, op)
            except Exception as exc:
                # Isolate a poison op: log, and dead-letter once its attempts are
                # exhausted so it can't spin forever and block everything behind it.
                log.exception("execute crashed for %s (%s): %s", cid[:8], op["command_type"], exc)
                self._fail_attempt(op, f"executor crashed: {exc}", pre_execute=False)
                continue
            self.q.mark_executed(cid, result)
            log.info("executed %s %s ok=%s confirmed=%s (%dms)",
                     cid[:8], op["command_type"],
                     result["ok"], result["confirmed"], result["duration_ms"])

    def _fail_attempt(self, op: dict, reason: str, pre_execute: bool) -> None:
        """Handle a failed op: count the attempt (unless already counted) and
        dead-letter it once attempts are exhausted; otherwise leave it queued to
        retry next tick."""
        cid = op["command_id"]
        if pre_execute:
            # bump_attempt hasn't run for this op yet — count it here.
            self.q.bump_attempt(cid)
        attempts_now = int(op.get("attempts") or 0) + 1  # op holds the pre-bump value
        if attempts_now >= self.s.max_execute_attempts:
            self.q.mark_executed(cid, {
                "ok": False,
                "confirmed": False,
                "duration_ms": 0,
                "response": f"dead-lettered after {attempts_now} attempts: {reason}"[:2000],
            })
            log.error("dead-lettered %s after %d attempts: %s", cid[:8], attempts_now, reason)
        else:
            log.warning("attempt %d/%d failed for %s: %s",
                        attempts_now, self.s.max_execute_attempts, cid[:8], reason)

    def _deliver_cue(self, op: dict) -> dict:
        """Deliver a must-air presenter cue to the studio on both chosen surfaces:
          1. the console overlay (via the shared local DB — the console polls it), and
          2. the RadioBOSS now-playing cast title (visible even if the console is closed).
        Delivery counts as success; the overlay dismiss is a purely local UX action.
        """
        payload = op.get("payload") or {}
        title = payload.get("title") or "MUST-AIR CUE"

        # display_until = air_at + display window (fallback: now + window).
        try:
            base = datetime.fromisoformat(payload["air_at"]) if payload.get("air_at") \
                else datetime.now(timezone.utc)
        except (ValueError, TypeError, KeyError):
            base = datetime.now(timezone.utc)
        display_until = (base + timedelta(seconds=self.s.cue_display_seconds)).isoformat()

        # Surface 1 — console overlay.
        try:
            self.q.add_cue({
                "command_id": op["command_id"],
                "title": title,
                "script": payload.get("script"),
                "air_at": payload.get("air_at"),
                "display_until": display_until,
            })
            self.q.prune_cues(datetime.now(timezone.utc).isoformat())
        except Exception as exc:  # noqa: BLE001 — never let a cue store failure kill the tick
            log.warning("presenter cue store failed: %s", exc)

        # Surface 2 — RadioBOSS cast title (best-effort).
        try:
            self.rb.execute("setcasttitle", {"title": "CUE: " + title})
        except Exception as exc:  # noqa: BLE001
            log.debug("cast-title cue failed: %s", exc)

        return {"ok": True, "confirmed": True, "duration_ms": 0, "response": "cue delivered"}

    def _flush_acks(self) -> None:
        acks = self.q.pending_acks()
        if not acks:
            return
        try:
            self.erp.ack(acks)
        except ErpError as exc:
            log.warning("ack flush failed (uplink down?), will retry: %s", exc)
            return
        self.q.clear_acks([a["command_id"] for a in acks])
        log.info("acked %d result(s)", len(acks))

    # ── listener SMS (studio-local presenter feed + reach) ─────────────
    def _poll_sms(self) -> None:
        if not self.sms:
            return
        now = time.monotonic()
        if now - self._last_sms_poll < self.s.sms_poll_interval:
            return
        self._last_sms_poll = now
        try:
            msgs = self.sms.poll()
        except Exception as exc:
            log.warning("sms poll failed: %s", exc)
            return
        added = self.q.add_messages(msgs) if msgs else 0
        if added:
            log.info("received %d new listener message(s)", added)

    def _flush_interactions(self) -> None:
        msgs = self.q.unposted_messages()
        if not msgs:
            return
        payload = []
        for m in msgs:
            item = {
                "phone": m["phone"], "name": m.get("name"), "content": m.get("content"),
                "source": m.get("source") or "SMS", "external_id": m.get("external_id"),
                "received_at": m.get("received_at"),
            }
            item.update(self._enrich(m))  # local-AI triage (category / sentiment / flag), if enabled
            payload.append(item)
        try:
            self.erp.report_interactions(payload)
        except ErpError as exc:
            log.warning("interaction report failed (uplink down?), will retry: %s", exc)
            return
        self.q.mark_posted([(m.get("source") or "SMS", m["external_id"]) for m in msgs])
        for m in msgs:  # enrichment done its job — free the cache
            self._ai_cache.pop(m.get("external_id"), None)
        log.info("registered %d listener interaction(s) as reach", len(msgs))

    def _enrich(self, m: dict) -> dict:
        """Local-AI enrichment for one message, cached by external_id so an
        uplink retry doesn't re-run inference. No-op when AI is disabled."""
        if not self.ai.enabled():
            return {}
        eid = m.get("external_id")
        if eid in self._ai_cache:
            return self._ai_cache[eid]
        enr = self.ai.enrich_message(m.get("content")) or {}
        if eid:
            self._ai_cache[eid] = enr
        return enr

    def _maybe_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat < self.s.heartbeat_interval:
            return
        self._last_heartbeat = now
        live = self.rb.playback_info()
        if live is not None and not live.get("prev"):
            try:  # last-played feeds the console's "Previous" card
                recent = self.rb.recently_played(1)
                if recent:
                    live["prev"] = recent[0]
            except Exception:  # noqa: BLE001
                pass
        telem = self._telemetry()
        try:
            resp = self.erp.heartbeat(healthy=live is not None, live=live, telemetry=telem)
        except ErpError as exc:
            record_heartbeat(self.q.db, heartbeat_scope(self.s), exc)
            log.debug("heartbeat failed: %s", exc)
            return
        record_heartbeat(self.q.db, heartbeat_scope(self.s))
        # Final authority: urgent cut/switch commands ride the heartbeat for low
        # latency — queue them like any other work (executed next tick, acked).
        urgent = resp.get("urgent") or []
        if urgent:
            self.q.add_work(urgent)
            log.warning("received %d urgent command(s)", len(urgent))
        self._report_dead_air(telem.get("audio"))
        self._flush_recordings()

    def _telemetry(self) -> dict:
        """Tier-B snapshot for the heartbeat: offline-buffer depth, host load,
        version, uptime and local clock (→ server-side drift). Host load is
        best-effort — omitted if psutil is unavailable."""
        work, acks = self.q.depth()
        t: dict = {
            "buffered_events": work,
            "pending_acks": acks,
            "app_version": __version__,
            "uptime_seconds": int(time.monotonic() - self._started),
            "gateway_time": datetime.now(timezone.utc).isoformat(),
        }
        if psutil is not None:
            try:
                t["cpu_pct"] = round(psutil.cpu_percent(interval=None))
                t["ram_pct"] = round(psutil.virtual_memory().percent)
                root = os.path.splitdrive(os.path.abspath("."))[0] + os.sep or os.sep
                t["disk_pct"] = round(psutil.disk_usage(root).percent)
            except Exception as exc:  # noqa: BLE001
                log.debug("host telemetry unavailable: %s", exc)
        # Encoder status rides the heartbeat: the cloud can't reach our localhost
        # RadioBOSS, so this is the only way a gateway station's stream health
        # reaches the operator console.
        try:
            encoders = self.rb.encoder_status()
            if encoders:
                t["encoders"] = encoders
        except Exception as exc:  # noqa: BLE001
            log.debug("encoder status unavailable: %s", exc)
        audio = self.audio.snapshot()  # Tier C — on-air level metering + silence
        if audio:
            t["audio"] = audio
        recording = self.recorder.snapshot()  # Tier C — as-aired logger status
        if recording:
            ts = self.transcriber.status()     # Whisper transcription status (folded in)
            if ts:
                recording["transcription"] = ts
            t["recording"] = recording
        if self._verification:  # Tier C — ACR verdict (expected vs heard)
            t["verification"] = self._verification
        if self.ai.enabled():   # local-AI status (model, health, active features)
            t["ai"] = self.ai.status()
        return t

    def _maybe_verify(self) -> None:
        """Fingerprint a window of live audio and compare the recognised creative
        to the one the automation says is playing. Raises CONTENT_MISMATCH when
        they diverge with confidence. EXPERIMENTAL — thresholds need field tuning."""
        if self.refindex is None:
            return
        now = time.monotonic()
        if now - self._last_verify < self.s.fp_verify_interval:
            return
        self._last_verify = now

        if self.manifest.loaded:  # (re)build the reference index when the approved set changes
            try:
                self.refindex.build(self.manifest.assets, self.s.media_folder)
            except Exception as exc:  # noqa: BLE001
                log.debug("fingerprint index build failed: %s", exc)
        if not self.refindex.ready():
            return

        got = self.audio.recent_samples(self.s.fp_window_seconds)
        if got is None:
            return
        samples, sr = got
        factor = max(1, round(sr / self.s.fp_sr))
        live_hashes = self.fingerprinter.fingerprint(samples[::factor])
        detected = self.refindex.match(live_hashes, self.s.fp_min_score)

        pb = self.rb.playback_info() or {}
        cur = pb.get("current") or {}
        expected = self.refindex.asset_for_filename(cur.get("filename") or cur.get("title"))

        verdict = self._verdict(expected, detected)
        self._verification = {"enabled": True, "expected": expected, "detected": detected, "verdict": verdict}
        if verdict == "FAIL":
            self._report_mismatch(expected, detected)

    @staticmethod
    def _verdict(expected: dict | None, detected: dict | None) -> str:
        if detected is None:
            return "NO_MATCH"        # couldn't recognise the sound (silence/unknown)
        if expected is None:
            return "DETECTED"        # recognised, but nothing scheduled to check against
        return "PASS" if detected["asset_id"] == expected["asset_id"] else "FAIL"

    def _report_mismatch(self, expected: dict, detected: dict) -> None:
        now = time.monotonic()
        if now - self._last_mismatch < 30.0:
            return
        self._last_mismatch = now
        try:
            self.erp.report_violations([{
                "type": "CONTENT_MISMATCH",
                "severity": "error",
                "summary": (f"On-air audio doesn't match the scheduled creative — "
                            f"expected {expected.get('filename')}, heard {detected.get('filename')}"),
                "detail": {"expected": expected, "detected": detected},
            }])
            log.warning("CONTENT MISMATCH — expected %s, heard %s (score %s)",
                        expected.get("filename"), detected.get("filename"), detected.get("score"))
        except ErpError as exc:
            log.debug("mismatch report failed (uplink down?): %s", exc)

    def _flush_recordings(self) -> None:
        """Report finalized as-aired segments to MediaERP (idempotent by filename)
        and prune expired ones. Best-effort — files stay on disk if the uplink is
        down and are re-reported next tick."""
        self.recorder.prune()
        segs = self.recorder.finalized_segments()
        if segs:
            try:
                self.erp.report_recordings(segs)
                self.recorder.mark_posted([s["filename"] for s in segs])
                log.info("logged %d as-aired recording segment(s)", len(segs))
            except ErpError as exc:
                log.debug("recording report failed (uplink down?): %s", exc)

        # Whisper transcripts (produced in the background) → attach to segments.
        transcripts = self.transcriber.pending()
        if transcripts:
            self._verify_ad_reads(transcripts)  # live-read ad check needs the transcript text
            try:
                self.erp.report_transcripts(transcripts)
                self.transcriber.mark_sent([tr["filename"] for tr in transcripts])
                log.info("posted %d transcript(s)", len(transcripts))
            except ErpError as exc:
                log.debug("transcript report failed (uplink down?): %s", exc)

    def _verify_ad_reads(self, transcripts: list[dict]) -> None:
        """Live-read verification: for each live-read ad, check whether its
        required script was actually read in a transcript, via the local LLM. A
        word-overlap prefilter keeps LLM calls to plausible matches only."""
        if not self.ai.enabled() or not self.manifest.live_reads:
            return
        detections: list[dict] = []
        for tr in transcripts:
            text = tr.get("text") or ""
            if not text:
                continue
            low = text.lower()
            for lr in self.manifest.live_reads:
                aid, script = lr.get("asset_id"), lr.get("script")
                if aid is None or not script:
                    continue
                key = (aid, tr["filename"])
                if key in self._ad_read_done:
                    continue
                words = self._significant_words(script)
                if not words:
                    continue
                overlap = sum(1 for w in words if w in low) / len(words)
                if overlap < 0.3:  # not plausibly read in this window; leave for another segment
                    continue
                snippet = self._snippet(text, low, words)
                verdict = self.ai.verify_ad_read(script, snippet)
                self._ad_read_done.add(key)
                if verdict:
                    detections.append({
                        "media_asset_id": aid, "filename": tr["filename"],
                        "verdict": verdict["verdict"], "coverage": verdict["coverage"],
                        "missing": verdict["missing"], "excerpt": snippet[:2000],
                    })
        if detections:
            try:
                self.erp.report_ad_reads(detections)
                log.info("verified %d live-read ad(s)", len(detections))
            except ErpError as exc:
                log.debug("ad-read report failed (uplink down?): %s", exc)
                for d in detections:  # allow a retry next batch
                    self._ad_read_done.discard((d["media_asset_id"], d["filename"]))

    @staticmethod
    def _significant_words(script: str) -> list[str]:
        return list({w for w in re.findall(r"[a-z0-9']+", script.lower()) if len(w) > 3})

    @staticmethod
    def _snippet(text: str, low: str, words: list[str]) -> str:
        positions = [low.find(w) for w in words if low.find(w) >= 0]
        pos = min(positions) if positions else 0
        start = max(0, pos - 300)
        return text[start:start + 1200]

    def _report_dead_air(self, audio: dict | None) -> None:
        """Raise a DEAD_AIR compliance violation while the signal is silent past
        the threshold. Throttled — the server dedupes onto one open row anyway,
        this just avoids a report every heartbeat."""
        if not audio or not audio.get("dead_air"):
            return
        now = time.monotonic()
        if now - self._last_dead_air < 15.0:
            return
        self._last_dead_air = now
        secs = int(audio.get("silent_seconds") or 0)
        try:
            self.erp.report_violations([{
                "type": "DEAD_AIR",
                "severity": "error",
                "summary": f"Dead air — silence for {secs}s (below {self.s.silence_dbfs:.0f} dBFS)",
                "detail": {"silent_seconds": audio.get("silent_seconds"), "level_dbfs": audio.get("level_dbfs")},
            }])
            log.warning("DEAD AIR reported — silent %ds", secs)
        except ErpError as exc:
            log.debug("dead-air report failed (uplink down?): %s", exc)

    def _maybe_publish(self) -> None:
        """Publish today's approved playlist once a day (Phase 4). Retried next
        tick if the uplink is down (we don't mark it published)."""
        today = _date.today().isoformat()
        if self._published_date == today:
            return
        try:
            self.publisher.publish(today)
            self._published_date = today
        except ErpError as exc:
            log.warning("schedule publish failed (uplink down?): %s", exc)

    # ── governance (Phase 6) ───────────────────────────────────────────
    def _maybe_sync_manifest(self) -> None:
        now = time.monotonic()
        if self.manifest.loaded and now - self._last_manifest < 300:
            return
        if self.manifest.sync():
            self._last_manifest = now
            # (re)configure host monitors from the synced policy
            self.usb = UsbMonitor(self.manifest.policy)
            self.process = ProcessMonitor(self.manifest.policy)
            self.folders = FolderPolicy(self.manifest.policy)
            self.usb.enforce_block()
            self.folders.enforce()
            # Pre-stage the approved creatives so uploads land on disk ahead of air.
            self._prefetch_creatives()

    def _prefetch_creatives(self) -> None:
        """Pull every creative the manifest lists onto media_folder (non-blocking).

        FileSync.stage() short-circuits files already present with a matching
        checksum, so this only downloads new/changed assets. This is what makes
        "upload a creative in MediaERP → it's on the studio disk" happen without
        waiting for the ad to be scheduled or inserted.
        """
        if not self.s.prefetch_manifest:
            return
        queued = 0
        for a in self.manifest.assets:
            if not a.get("asset_id") or not a.get("filename"):
                continue
            status, _local, _reason = self.filesync.stage({
                "asset_id": a["asset_id"],
                "filename": a["filename"],
                "checksum": a.get("checksum"),
            })
            if status == PENDING:
                queued += 1
        if queued:
            log.info("prefetch: queued %d creative(s) for download", queued)

    def _governance(self) -> None:
        self._maybe_sync_manifest()
        if not self.manifest.loaded:
            return

        violations: list[dict] = []
        try:
            violations += self.reconciler.check()
        except Exception as exc:
            log.debug("reconcile error: %s", exc)
        violations += self.usb.poll()
        violations += self.process.poll()
        if not violations:
            return

        # Auto-cut unapproved content the moment it's seen (final authority).
        if self.manifest.policy.get("auto_cut"):
            unapproved = [v for v in violations
                          if v.get("type") == "UNAPPROVED_CONTENT" and v.get("position")]
            positions = [v["position"] for v in unapproved]
            if positions:
                # Only skip ('next') when the track ON AIR is itself unapproved —
                # otherwise deleting the queued items is enough, and an
                # unconditional 'next' would cut a legitimate track that happens
                # to be playing right now.
                cur = (self.rb.playback_info() or {}).get("current") or {}
                cur_fn = (cur.get("filename") or "").lower()
                cur_ct = (cur.get("casttitle") or "").lower()
                cutting_on_air = any(
                    (cur_fn and (v["detail"].get("filename") or "").lower() == cur_fn)
                    or (cur_ct and (v["detail"].get("casttitle") or "").lower() == cur_ct)
                    for v in unapproved
                )
                # Delete highest position first so lower indices don't shift.
                self.rb.execute("delete", {"positions": sorted(positions, reverse=True)})
                if cutting_on_air:
                    self.rb.execute("command", {"cmd": "next"})  # the on-air track is unapproved
                for v in unapproved:
                    v["action_taken"] = "AUTO_CUT"
                log.warning("auto-cut %d unapproved item(s)%s",
                            len(positions), " (incl. on-air)" if cutting_on_air else "")

        # Report (strip the local-only 'position' hint — not a server field).
        payload = [{k: val for k, val in v.items() if k != "position"} for v in violations]
        try:
            self.erp.report_violations(payload)
        except ErpError as exc:
            log.warning("violation report failed (uplink down?): %s", exc)

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep so a stop signal is honoured promptly."""
        waited = 0.0
        while waited < seconds and not self._stop:
            time.sleep(min(0.5, seconds - waited))
            waited += 0.5
