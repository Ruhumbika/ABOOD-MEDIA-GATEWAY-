"""Identity-based content classification (Phase 6).

Separates ads from songs — and approved from unapproved content — by IDENTITY,
not by listening to the audio: it matches each track against the approved
manifest by filename, and by SHA-256 when the file is on disk. Anything not in
the manifest is 'unapproved' (the actionable signal). Audio fingerprinting is
explicitly out of scope.
"""

from __future__ import annotations

import hashlib
import os


class ContentClassifier:
    def __init__(self, manifest) -> None:
        self.m = manifest

    def classify(self, track: dict) -> dict:
        """Return {'approved': bool, 'type': str|None} for a playlist track."""
        filename = track.get("filename") or ""
        base = os.path.basename(filename).lower() if filename else ""

        # 1. by filename (fast)
        if base and base in self.m.by_filename:
            return {"approved": True, "type": self.m.by_filename[base]}

        # 2. by content hash, when the file is on disk (catches a renamed file
        #    that is genuinely an approved asset; also detects a swapped file
        #    whose name matches but bytes don't).
        if filename and os.path.isfile(filename):
            digest = self._sha256(filename)
            if digest in self.m.by_checksum:
                return {"approved": True, "type": self.m.by_checksum[digest]}

        return {"approved": False, "type": None}

    @staticmethod
    def _sha256(path: str) -> str:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
        except OSError:
            return ""
        return h.hexdigest()
