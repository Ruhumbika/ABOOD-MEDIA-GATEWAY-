"""Compliance recorder (Tier C, sub-phase C2) — the as-aired logger.

Runs ffmpeg to capture the on-air audio into rolling, timestamped MP3 segments
(hourly by default) under a retention window — the legal "prove what you aired"
record. Each finalized segment is reported to MediaERP for the audit trail, and
a live status snapshot (recording?, today's hours, disk headroom) rides the
heartbeat.

Degrades to a clean no-op when disabled or when ffmpeg / an input source is
unavailable — the daemon runs fine without it.

Input source (`GATEWAY_RECORD_SOURCE`):
  - "stream" (default): the public broadcast stream URL (what listeners hear —
    the truest compliance record). Needs `GATEWAY_MONITOR_STREAM_URL`.
  - "device": a Windows DirectShow audio device (a feed off the mixer).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("gateway.recorder")


class ComplianceRecorder:
    def __init__(self, settings) -> None:
        self.s = settings
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._posted: set[str] = set()
        self._last_prune = 0.0

    # ── availability ────────────────────────────────────────────────────
    def _ffmpeg(self) -> str | None:
        cand = self.s.ffmpeg_path or "ffmpeg"
        return cand if (os.path.isfile(cand) or shutil.which(cand)) else None

    def _input_args(self) -> list[str] | None:
        src = self.s.record_source
        if src == "device" and self.s.record_device:
            return ["-f", "dshow", "-i", f"audio={self.s.record_device}"]
        if self.s.monitor_stream_url:  # "stream" (or fallback)
            return ["-i", self.s.monitor_stream_url]
        return None

    def available(self) -> bool:
        return self._ffmpeg() is not None and self._input_args() is not None

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        if not getattr(self.s, "record_enabled", False):
            return
        if not self.available():
            log.warning("recorder enabled but ffmpeg or an input source is missing — recording OFF")
            return
        os.makedirs(self.s.record_folder, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()
        log.info("compliance recorder started (source=%s → %s, %ss segments, %dd retention)",
                 self.s.record_source, self.s.record_folder,
                 self.s.record_segment_seconds, self.s.record_retention_days)

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        pattern = os.path.join(self.s.record_folder, "%Y-%m-%d_%H-%M-%S.mp3")
        while not self._stop.is_set():
            cmd = [
                self._ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
                *self._input_args(),
                "-f", "segment", "-segment_time", str(self.s.record_segment_seconds),
                "-strftime", "1", "-c:a", "libmp3lame", "-b:a", self.s.record_bitrate, pattern,
            ]
            try:
                self._proc = subprocess.Popen(cmd)
                self._proc.wait()
            except Exception as exc:  # noqa: BLE001
                log.warning("recorder ffmpeg failed: %s", exc)
            if not self._stop.is_set():
                time.sleep(3.0)  # backoff, then restart (e.g. the stream dropped)

    # ── maintenance / audit ─────────────────────────────────────────────
    def prune(self) -> None:
        """Delete segments past the retention window. Throttled to hourly."""
        now = time.monotonic()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        cutoff = time.time() - self.s.record_retention_days * 86400
        try:
            for name in os.listdir(self.s.record_folder):
                if not name.endswith(".mp3"):
                    continue
                p = os.path.join(self.s.record_folder, name)
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    self._posted.discard(name)
                    log.info("pruned expired recording %s", name)
        except FileNotFoundError:
            pass

    def _all_segments(self) -> list[str]:
        try:
            return sorted(f for f in os.listdir(self.s.record_folder) if f.endswith(".mp3"))
        except FileNotFoundError:
            return []

    def finalized_segments(self) -> list[dict]:
        """Completed segments not yet reported (all but the newest, still open)."""
        files = self._all_segments()
        out: list[dict] = []
        for name in files[:-1]:  # skip the segment currently being written
            if name in self._posted:
                continue
            p = os.path.join(self.s.record_folder, name)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            out.append({
                "filename": name,
                "started_at": self._parse_start(name),
                "size_bytes": size,
                "duration_seconds": self.s.record_segment_seconds,
            })
        return out

    def mark_posted(self, filenames: list[str]) -> None:
        self._posted.update(filenames)

    @staticmethod
    def _parse_start(name: str) -> str | None:
        try:
            dt = datetime.strptime(name.replace(".mp3", ""), "%Y-%m-%d_%H-%M-%S")
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            return None

    def snapshot(self) -> dict | None:
        if not getattr(self.s, "record_enabled", False):
            return None
        recording = self._proc is not None and self._proc.poll() is None
        files = self._all_segments()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_secs = sum(self.s.record_segment_seconds for f in files if f.startswith(today))
        snap = {
            "enabled": True,
            "recording": recording,
            "today_seconds": today_secs,
            "segments": len(files),
            "retention_days": self.s.record_retention_days,
            "current_file": files[-1] if files else None,
        }
        try:
            total, used, free = shutil.disk_usage(self.s.record_folder)
            snap["disk_free_gb"] = round(free / 1e9, 1)
            snap["disk_used_pct"] = round(100 * used / total)
        except Exception:  # noqa: BLE001
            pass
        return snap
