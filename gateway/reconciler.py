"""Playlist reconciliation (Phase 6).

Reads the live RadioBOSS playlist and flags any item that isn't approved
content — the detection half of "only approved content on air". Each flagged
item becomes a violation (carrying its playlist position so the worker can
auto-cut it per policy).
"""

from __future__ import annotations

import logging

log = logging.getLogger("gateway.reconciler")


class PlaylistReconciler:
    def __init__(self, rb, classifier) -> None:
        self.rb = rb
        self.classifier = classifier

    def check(self) -> list[dict]:
        violations: list[dict] = []
        for track in self.rb.playlist():
            verdict = self.classifier.classify(track)
            if verdict["approved"]:
                continue
            label = track.get("casttitle") or track.get("title") or track.get("filename") or "?"
            violations.append({
                "type": "UNAPPROVED_CONTENT",
                "severity": "error",
                "summary": f"Unapproved item in playlist: {label}",
                "detail": {
                    "filename": track.get("filename"),
                    "title": track.get("title"),
                    "casttitle": track.get("casttitle"),
                    "position": track.get("position"),
                },
                # local-only hint for the worker's auto-cut (stripped before report)
                "position": track.get("position"),
            })
        if violations:
            log.warning("reconcile: %d unapproved item(s) in playlist", len(violations))
        return violations
