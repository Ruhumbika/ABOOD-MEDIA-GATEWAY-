"""Audio monitoring (Tier C, sub-phase C1) — level metering + silence detection.

Captures the station's on-air audio in a background thread and computes rolling
RMS / peak levels (dBFS), left/right balance, and how long the signal has been
below the silence floor (→ dead-air alarm). Only computed NUMBERS leave the
studio — never audio (the recorder, C2, handles capture-to-disk separately).

Everything degrades to a clean no-op when the optional audio stack
(`numpy` + `sounddevice`) or a capture device is unavailable, so the daemon runs
fine with Tier C disabled or on a box without an audio device — mirroring the
guarded `psutil` telemetry.

Source (config `GATEWAY_AUDIO_SOURCE`):
  - "loopback" (default): WASAPI loopback of the default OUTPUT — what is
    actually leaving the studio PC's soundcard. Windows only.
  - "device": a named/'numbered input device (a physical feed off the mixer).
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time

log = logging.getLogger("gateway.audio")

try:  # optional — install with `pip install .[audio]`
    import numpy as np  # type: ignore
    import sounddevice as sd  # type: ignore
except Exception:  # noqa: BLE001
    np = None  # type: ignore
    sd = None  # type: ignore


def _dbfs(rms: float) -> float:
    """Linear RMS/peak (0..1) → dBFS, floored at -120 for silence."""
    if rms <= 1e-9:
        return -120.0
    return round(max(-120.0, 20.0 * math.log10(min(1.0, rms))), 1)


class AudioMonitor:
    def __init__(self, settings) -> None:
        self.s = settings
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._snap: dict = {"available": False}
        self._silent_since: float | None = None
        self._sr: int = int(settings.audio_samplerate)
        # Rolling mono buffer (~8 s) for the fingerprinter (Tier C, C3).
        self._buf: collections.deque = collections.deque(maxlen=max(1, int(8000 / max(1, settings.audio_block_ms))))

    def available(self) -> bool:
        return np is not None and sd is not None

    def start(self) -> None:
        if not getattr(self.s, "audio_enabled", False):
            return
        if not self.available():
            log.warning("audio monitoring enabled but numpy/sounddevice missing — run: pip install '.[audio]'")
            return
        self._thread = threading.Thread(target=self._run, name="audio-monitor", daemon=True)
        self._thread.start()
        log.info("audio monitor started (source=%s, silence=%.0f dBFS / %.0fs)",
                 self.s.audio_source, self.s.silence_dbfs, self.s.silence_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def snapshot(self) -> dict | None:
        """Latest metering snapshot, or None if audio isn't being captured."""
        with self._lock:
            return dict(self._snap) if self._snap.get("available") else None

    def recent_samples(self, seconds: float):
        """Last `seconds` of mono audio as (np.ndarray, sample_rate) for the
        fingerprinter, or None. Shares the [audio] numpy dependency."""
        if np is None:
            return None
        with self._lock:
            blocks = list(self._buf)
        if not blocks:
            return None
        data = np.concatenate(blocks)
        n = int(seconds * self._sr)
        return (data[-n:].copy() if n and len(data) > n else data, self._sr)

    # ── capture thread ──────────────────────────────────────────────────
    def _run(self) -> None:
        sr = int(self.s.audio_samplerate)
        block = max(1, int(sr * self.s.audio_block_ms / 1000))
        device = self.s.audio_device or None
        extra = None
        if self.s.audio_source == "loopback":
            try:  # WASAPI loopback captures the OUTPUT device (Windows only)
                extra = sd.WasapiSettings(loopback=True)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                log.warning("WASAPI loopback unavailable — falling back to default input device")
        try:
            with sd.InputStream(
                samplerate=sr, blocksize=block, channels=2, dtype="float32",
                device=device, extra_settings=extra, callback=self._on_block,
            ):
                while not self._stop.is_set():
                    time.sleep(0.2)
        except Exception as exc:  # noqa: BLE001
            log.warning("audio capture stopped: %s", exc)
            with self._lock:
                self._snap = {"available": False, "error": str(exc)[:120]}

    def _on_block(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        try:
            data = np.asarray(indata, dtype=np.float32)
            if data.ndim == 1:
                data = data.reshape(-1, 1)
            left = data[:, 0]
            right = data[:, 1] if data.shape[1] > 1 else left

            rms = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
            peak = float(np.max(np.abs(data))) if data.size else 0.0
            level_db = _dbfs(rms)
            silent = level_db < self.s.silence_dbfs

            now = time.monotonic()
            if silent:
                if self._silent_since is None:
                    self._silent_since = now
                silent_secs = now - self._silent_since
            else:
                self._silent_since = None
                silent_secs = 0.0

            mono = np.mean(data, axis=1).astype(np.float32) if data.ndim > 1 else data
            with self._lock:
                self._buf.append(mono.copy())
                self._snap = {
                    "available": True,
                    "level_dbfs": level_db,
                    "peak_dbfs": _dbfs(peak),
                    "left_dbfs": _dbfs(float(np.sqrt(np.mean(np.square(left))))),
                    "right_dbfs": _dbfs(float(np.sqrt(np.mean(np.square(right))))),
                    "silent": silent,
                    "silent_seconds": round(silent_secs, 1),
                    "dead_air": silent_secs >= self.s.silence_seconds,
                }
        except Exception as exc:  # noqa: BLE001
            log.debug("audio block error: %s", exc)
