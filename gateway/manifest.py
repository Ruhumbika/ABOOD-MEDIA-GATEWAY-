"""Approved-content manifest (Phase 6).

Synced from MediaERP, it's the on-site source of truth for what is allowed on
air: each approved asset keyed by checksum and by filename → its type
(AD / SONG / JINGLE / …), plus the governance policy (allowed folders, USB
block, app allowlist, auto-cut). This is what makes identity-based ad/song
separation possible — no audio fingerprinting.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("gateway.manifest")


class ApprovedManifest:
    def __init__(self, erp) -> None:
        self.erp = erp
        self.by_checksum: dict[str, str] = {}   # sha256 → type
        self.by_filename: dict[str, str] = {}   # basename(lower) → type
        self.assets: list[dict] = []            # raw asset list (for pre-fetch)
        self.live_reads: list[dict] = []        # [{asset_id, title, script}] for live-read verification
        self.policy: dict = {}
        self.loaded = False

    def sync(self) -> bool:
        try:
            data = self.erp.get_manifest()
        except Exception as exc:  # ErpError / transport
            log.warning("manifest sync failed: %s", exc)
            return False

        by_cs: dict[str, str] = {}
        by_fn: dict[str, str] = {}
        for asset in data.get("assets", []):
            atype = asset.get("type")
            if asset.get("checksum"):
                by_cs[asset["checksum"]] = atype
            if asset.get("filename"):
                # Key by BASENAME (lower) — the classifier looks up the playlist
                # track's basename, and every synced creative lands in the media
                # folder by basename. Keying on a server-side path/prefix here
                # would miss every match → false "unapproved" → wrongful auto-cut.
                by_fn[os.path.basename(asset["filename"]).lower()] = atype

        self.by_checksum = by_cs
        self.by_filename = by_fn
        self.assets = data.get("assets", [])
        self.live_reads = [
            {"asset_id": a.get("asset_id"), "title": a.get("title"), "script": a.get("script")}
            for a in self.assets if a.get("is_live_read") and a.get("script")
        ]
        self.policy = data.get("policy") or {}
        self.loaded = True
        log.info("manifest synced: %d approved asset(s)", len(data.get("assets", [])))
        return True
