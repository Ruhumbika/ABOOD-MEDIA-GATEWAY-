"""File transfer (Phase 3).

RadioBOSS plays files by disk path, so a creative must physically exist on the
studio disk before it can air. FileSync ensures that: it checks the local copy
(by SHA-256), downloads from MediaERP if missing or changed, verifies integrity
against the expected checksum, and returns the local path.

Two entry points:

- ``stage(item)`` — **non-blocking**, used on the hot playout path. It returns
  immediately with the file's readiness and kicks off a background download if
  needed. This keeps a slow/large download from stalling every other command in
  the tick (including the urgent cut/switch commands that ride the heartbeat).
- ``ensure(item)`` — **blocking**, used by the once-a-day schedule publisher
  where waiting for each file is fine.

An asset already present with a matching hash is a no-op — no re-download — and
verified hashes are cached so we don't re-read the whole file every tick.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("gateway.filesync")

# stage() status values
READY = "ready"        # file is on disk and (when known) hash-verified
PENDING = "pending"    # a background download is in flight — try again next tick
FAILED = "failed"      # the last download attempt failed (surfaced once)


class FileSync:
    def __init__(self, erp, media_folder: str, download_workers: int = 2) -> None:
        self.erp = erp
        self.media_folder = media_folder
        os.makedirs(media_folder, exist_ok=True)

        self._pool = ThreadPoolExecutor(max_workers=max(1, download_workers),
                                        thread_name_prefix="filesync")
        self._lock = threading.Lock()
        self._inflight: set = set()          # asset_ids with a running download
        self._failed: dict = {}              # asset_id → reason (unsurfaced failure)
        self._verified: dict[str, str] = {}  # local_path → verified sha256

    # ── non-blocking staging (hot path) ────────────────────────────────
    def stage(self, item: dict) -> tuple[str, str | None, str | None]:
        """(status, local_path, reason). Never blocks on the network."""
        asset_id = item.get("asset_id")
        filename = item.get("filename")
        if not asset_id or not filename:
            return (FAILED, None, "item missing asset_id/filename")

        local = self._local_for(filename)
        if self._is_ready(local, item.get("checksum")):
            return (READY, local, None)

        with self._lock:
            # A completed-with-error download: surface it ONCE (so the worker can
            # count an attempt / eventually dead-letter), then allow a fresh try.
            if asset_id in self._failed:
                reason = self._failed.pop(asset_id)
                return (FAILED, None, reason)
            if asset_id in self._inflight:
                return (PENDING, None, None)
            self._inflight.add(asset_id)
            self._pool.submit(self._download_task, dict(item))
        return (PENDING, None, None)

    def _download_task(self, item: dict) -> None:
        asset_id = item.get("asset_id")
        try:
            self._download_sync(item)
        except Exception as exc:  # ErpError, integrity, OS
            log.warning("download failed for asset %s: %s", asset_id, exc)
            with self._lock:
                self._failed[asset_id] = str(exc)
        finally:
            with self._lock:
                self._inflight.discard(asset_id)

    # ── blocking ensure (publisher batch path) ─────────────────────────
    def ensure(self, item: dict) -> str | None:
        """Ensure the item's creative is on disk; return the local path or None.
        Blocks until the download completes."""
        asset_id = item.get("asset_id")
        filename = item.get("filename")
        if not asset_id or not filename:
            return None
        local = self._local_for(filename)
        if self._is_ready(local, item.get("checksum")):
            return local
        try:
            return self._download_sync(item)
        except Exception as exc:
            log.warning("download failed for asset %s: %s", asset_id, exc)
            return None

    # ── internals ──────────────────────────────────────────────────────
    def _download_sync(self, item: dict) -> str:
        asset_id = item["asset_id"]
        expected = item.get("checksum")
        local = self._local_for(item["filename"])
        tmp = local + ".part"

        actual, dl_checksum = self.erp.download_file_to(asset_id, tmp)

        if expected and actual != expected:
            self._safe_remove(tmp)
            raise ValueError(f"integrity fail: expected {expected} got {actual}")
        if dl_checksum and actual != dl_checksum:
            self._safe_remove(tmp)
            raise ValueError(f"integrity fail: X-Checksum {dl_checksum} got {actual}")

        os.replace(tmp, local)  # atomic
        with self._lock:
            self._verified[local] = actual
        log.info("synced asset %s → %s", asset_id, local)
        return local

    def _is_ready(self, local: str, expected: str | None) -> bool:
        if not os.path.isfile(local):
            return False
        if not expected:
            return True  # present, and we have nothing to verify against
        with self._lock:
            if self._verified.get(local) == expected:
                return True
        # Hash once (e.g. a pre-existing file) and cache the result.
        actual = self._sha256_file(local)
        if actual == expected:
            with self._lock:
                self._verified[local] = actual
            return True
        return False  # present but wrong bytes → a re-download is warranted

    def _local_for(self, filename: str) -> str:
        return os.path.join(self.media_folder, os.path.basename(filename))

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _sha256_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
