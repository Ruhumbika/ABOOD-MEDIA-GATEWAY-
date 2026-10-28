"""Audio fingerprinting / ACR (Tier C, sub-phase C3) — EXPERIMENTAL.

A self-contained landmark ("constellation") fingerprinter in the Shazam family,
built on numpy only. It answers: does the sound on air actually match the
creative the automation *claims* is playing? — catching a swapped/disguised ad
that identity checks (checksum/filename) can't, because the bytes look right.

Pipeline:
  1. STFT magnitude spectrogram of mono audio (downsampled).
  2. Per-frame spectral peaks (top-K bins) → a constellation of (time, freq).
  3. Hash peak PAIRS within a target zone → (f1, f2, dt) packed to an int, with
     the anchor time. Robust to noise/time-shift.
  4. Reference creatives are fingerprinted once (decoded via ffmpeg) into an
     in-memory index (persisted to SQLite so restarts skip re-decoding).
  5. Live audio is fingerprinted and matched: shared hashes vote on a consistent
     time-offset per asset; the best aligned count is the match score.

⚠ EXPERIMENTAL: thresholds (peaks/fan-out/min-score) need tuning against real
station audio. Degrades to a no-op without numpy or ffmpeg.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from collections import Counter, defaultdict

log = logging.getLogger("gateway.fingerprint")

try:  # shares the [audio] extra with the meter
    import numpy as np  # type: ignore
except Exception:  # noqa: BLE001
    np = None  # type: ignore


# ── landmark fingerprinter ──────────────────────────────────────────────
class Fingerprinter:
    def __init__(self, sr: int = 11025, win: int = 1024, hop: int = 512,
                 peaks_per_frame: int = 5, fan: int = 8, min_amp: float = 1e-3) -> None:
        self.sr = sr
        self.win = win
        self.hop = hop
        self.peaks_per_frame = peaks_per_frame
        self.fan = fan
        self.min_amp = min_amp
        self._window = np.hanning(win) if np is not None else None

    def fingerprint(self, samples) -> list[tuple[int, int]]:
        if np is None or samples is None or len(samples) < self.win:
            return []
        spec = self._spectrogram(np.asarray(samples, dtype=np.float32))
        peaks = self._peaks(spec)
        return self._pairs(peaks)

    def _spectrogram(self, samples):
        n = 1 + (len(samples) - self.win) // self.hop
        idx = np.arange(self.win)[None, :] + self.hop * np.arange(n)[:, None]
        frames = samples[idx] * self._window
        return np.abs(np.fft.rfft(frames, axis=1))  # (frames, bins)

    def _peaks(self, spec) -> list[tuple[int, int]]:
        peaks: list[tuple[int, int]] = []
        k = self.peaks_per_frame
        for t in range(spec.shape[0]):
            frame = spec[t]
            if frame.size == 0:
                continue
            top = np.argpartition(frame, -k)[-k:] if frame.size > k else np.arange(frame.size)
            for f in top:
                if frame[f] >= self.min_amp:
                    peaks.append((t, int(f)))
        peaks.sort()
        return peaks

    def _pairs(self, peaks: list[tuple[int, int]]) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        n = len(peaks)
        for i in range(n):
            t1, f1 = peaks[i]
            for j in range(i + 1, min(i + 1 + self.fan, n)):
                t2, f2 = peaks[j]
                dt = t2 - t1
                if dt <= 0 or dt > 1023:
                    continue
                h = (f1 & 0x3FF) | ((f2 & 0x3FF) << 10) | ((dt & 0x3FF) << 20)
                out.append((h, t1))
        return out


# ── reference index (approved creatives) + matcher ──────────────────────
class ReferenceIndex:
    def __init__(self, db_path: str, fingerprinter: Fingerprinter, ffmpeg: str = "ffmpeg") -> None:
        self.db_path = db_path
        self.fp = fingerprinter
        self.ffmpeg = ffmpeg or "ffmpeg"
        self.table: dict[int, list[tuple[int, int]]] = defaultdict(list)  # hash → [(asset_id, offset)]
        self.meta: dict[int, dict] = {}          # asset_id → {filename, type}
        self.by_filename: dict[str, int] = {}    # basename(lower) → asset_id
        self.signature: str | None = None
        self._load()

    def ready(self) -> bool:
        return np is not None and bool(self.table)

    @staticmethod
    def _signature(assets: list[dict]) -> str:
        return ",".join(sorted(str(a.get("checksum") or a.get("asset_id")) for a in assets))

    def build(self, assets: list[dict], media_folder: str) -> bool:
        """(Re)build the index from approved assets present on disk. Skips when
        the approved set is unchanged. Returns True if a rebuild ran."""
        if np is None:
            return False
        sig = self._signature(assets)
        if sig == self.signature and self.table:
            return False

        table: dict[int, list[tuple[int, int]]] = defaultdict(list)
        meta: dict[int, dict] = {}
        by_fn: dict[str, int] = {}
        indexed = 0
        for a in assets:
            asset_id = a.get("asset_id")
            fn = a.get("filename")
            if asset_id is None or not fn:
                continue
            base = os.path.basename(fn)
            path = os.path.join(media_folder, base)
            if not os.path.isfile(path):
                continue  # creative not staged locally yet
            samples = self._decode(path)
            if samples is None or len(samples) < self.fp.win:
                continue
            for h, off in self.fp.fingerprint(samples):
                table[h].append((int(asset_id), off))
            meta[int(asset_id)] = {"filename": base, "type": a.get("type")}
            by_fn[base.lower()] = int(asset_id)
            indexed += 1

        self.table, self.meta, self.by_filename, self.signature = table, meta, by_fn, sig
        self._save()
        log.info("fingerprint index built: %d creative(s), %d hashes", indexed, len(table))
        return True

    def match(self, live_hashes: list[tuple[int, int]], min_score: int) -> dict | None:
        """Best matching asset by aligned-offset voting, or None below min_score."""
        if not live_hashes or not self.table:
            return None
        votes: dict[int, Counter] = defaultdict(Counter)
        for h, t in live_hashes:
            for asset_id, off in self.table.get(h, ()):  # noqa: B905
                votes[asset_id][off - t] += 1
        best_id, best_score = None, 0
        for asset_id, c in votes.items():
            _, score = c.most_common(1)[0]
            if score > best_score:
                best_id, best_score = asset_id, score
        if best_id is None or best_score < min_score:
            return None
        m = self.meta.get(best_id, {})
        return {"asset_id": best_id, "filename": m.get("filename"), "type": m.get("type"), "score": int(best_score)}

    def asset_for_filename(self, filename: str | None) -> dict | None:
        if not filename:
            return None
        aid = self.by_filename.get(os.path.basename(filename).lower())
        if aid is None:
            return None
        m = self.meta.get(aid, {})
        return {"asset_id": aid, "filename": m.get("filename"), "type": m.get("type")}

    # ── ffmpeg decode + SQLite persistence ──────────────────────────────
    def _decode(self, path: str):
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", path, "-ac", "1", "-ar", str(self.fp.sr), "-f", "f32le", "-"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=60).stdout
        except Exception as exc:  # noqa: BLE001
            log.debug("decode failed for %s: %s", path, exc)
            return None
        return np.frombuffer(out, dtype=np.float32) if out else None

    def _db(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        db = sqlite3.connect(self.db_path, isolation_level=None)
        db.executescript(
            "CREATE TABLE IF NOT EXISTS fp (hash INTEGER, asset_id INTEGER, offset INTEGER);"
            "CREATE INDEX IF NOT EXISTS ix_fp_hash ON fp(hash);"
            "CREATE TABLE IF NOT EXISTS meta (asset_id INTEGER PRIMARY KEY, filename TEXT, type TEXT);"
            "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);"
        )
        return db

    def _save(self) -> None:
        try:
            db = self._db()
            db.execute("BEGIN")
            db.execute("DELETE FROM fp")
            db.execute("DELETE FROM meta")
            rows = ((h, aid, off) for h, lst in self.table.items() for aid, off in lst)
            db.executemany("INSERT INTO fp (hash, asset_id, offset) VALUES (?,?,?)", rows)
            db.executemany("INSERT OR REPLACE INTO meta (asset_id, filename, type) VALUES (?,?,?)",
                           ((aid, m.get("filename"), m.get("type")) for aid, m in self.meta.items()))
            db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('signature', ?)", (self.signature,))
            db.execute("COMMIT")
            db.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("fingerprint index save failed: %s", exc)

    def _load(self) -> None:
        if not os.path.isfile(self.db_path):
            return
        try:
            db = self._db()
            for aid, fn, ty in db.execute("SELECT asset_id, filename, type FROM meta"):
                self.meta[aid] = {"filename": fn, "type": ty}
                if fn:
                    self.by_filename[fn.lower()] = aid
            for h, aid, off in db.execute("SELECT hash, asset_id, offset FROM fp"):
                self.table[h].append((aid, off))
            row = db.execute("SELECT v FROM kv WHERE k='signature'").fetchone()
            self.signature = row[0] if row else None
            db.close()
            if self.table:
                log.info("fingerprint index loaded from cache: %d hashes", len(self.table))
        except Exception as exc:  # noqa: BLE001
            log.debug("fingerprint index load failed: %s", exc)
