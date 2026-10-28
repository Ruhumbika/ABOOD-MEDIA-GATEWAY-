"""As-aired transcription (Tier C follow-on) — local speech-to-text via Whisper.

Transcribes the compliance recorder's finalized MP3 segments with faster-whisper
(local, CPU or GPU) so the as-aired audio becomes SEARCHABLE text, and flags
banned/profane language that went to air. Runs as a BACKGROUND BATCH — one
segment at a time, off the playout hot path — because transcription is slow.
Audio + transcripts stay on-site; only the resulting text syncs up.

Degrades to a clean no-op without faster-whisper or when disabled. Depends on
the C2 recorder producing segments (nothing to transcribe otherwise).

⚠ Model size trades accuracy for CPU: "small" is a good multilingual default
(handles Swahili + English); "medium"/"large-v3" are heavier.
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("gateway.transcriber")

try:  # optional — install with `pip install .[whisper]`
    from faster_whisper import WhisperModel  # type: ignore
except Exception:  # noqa: BLE001
    WhisperModel = None  # type: ignore


class Transcriber:
    def __init__(self, settings) -> None:
        self.s = settings
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._model = None
        self._lock = threading.Lock()
        self._results: dict[str, dict] = {}   # filename → transcript (awaiting post)
        self._processed: set[str] = set()      # filenames already handled
        self._banned = [w.strip().lower() for w in (settings.transcribe_banned_words or "").split(",") if w.strip()]

    def available(self) -> bool:
        return WhisperModel is not None

    def start(self) -> None:
        if not getattr(self.s, "transcribe_enabled", False):
            return
        if not self.available():
            log.warning("transcription enabled but faster-whisper missing — run: pip install '.[whisper]'")
            return
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self._thread.start()
        log.info("transcriber started (model=%s, device=%s)", self.s.whisper_model, self.s.whisper_device)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # ── background batch loop ───────────────────────────────────────────
    def _run(self) -> None:
        try:
            self._model = WhisperModel(
                self.s.whisper_model, device=self.s.whisper_device, compute_type=self.s.whisper_compute_type)
        except Exception as exc:  # noqa: BLE001
            log.warning("whisper model load failed: %s", exc)
            return
        while not self._stop.is_set():
            filename = self._next_file()
            if filename is None:
                self._stop.wait(self.s.transcribe_poll_seconds)
                continue
            self._transcribe_one(filename)

    def _next_file(self) -> str | None:
        try:
            files = sorted(f for f in os.listdir(self.s.record_folder) if f.endswith(".mp3"))
        except FileNotFoundError:
            return None
        for f in files[:-1]:  # skip the segment still being written
            if f not in self._processed and f not in self._results:
                return f
        return None

    def _transcribe_one(self, filename: str) -> None:
        path = os.path.join(self.s.record_folder, filename)
        try:
            segments, info = self._model.transcribe(path, language=self.s.whisper_language or None)
            text = " ".join(s.text.strip() for s in segments).strip()
            language = getattr(info, "language", None)
        except Exception as exc:  # noqa: BLE001
            log.warning("transcription failed for %s: %s", filename, exc)
            with self._lock:
                self._processed.add(filename)  # don't hammer a bad file
            return
        low = text.lower()
        matched = [w for w in self._banned if w in low]
        result = {
            "filename": filename,
            "text": text[:20000],
            "language": language,
            "flagged": bool(matched),
            "flagged_terms": matched,
        }
        with self._lock:
            self._results[filename] = result
            self._processed.add(filename)
        log.info("transcribed %s (%d chars, lang=%s%s)", filename, len(text), language,
                 ", FLAGGED" if matched else "")

    # ── worker interface ────────────────────────────────────────────────
    def pending(self) -> list[dict]:
        with self._lock:
            return list(self._results.values())

    def mark_sent(self, filenames: list[str]) -> None:
        with self._lock:
            for f in filenames:
                self._results.pop(f, None)

    def status(self) -> dict | None:
        if not getattr(self.s, "transcribe_enabled", False):
            return None
        with self._lock:
            pending = len(self._results)
            done = len(self._processed)
        return {
            "enabled": True,
            "available": self.available(),
            "model": self.s.whisper_model,
            "pending": pending,
            "transcribed": done,
        }
