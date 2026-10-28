"""Running-app monitoring (Windows-ish). No-op without `psutil`.

Reports processes matching a configured blocklist. NOTE: strict app control is
AppLocker / WDAC's job — this is a lightweight detector for known-bad apps, kept
conservative to avoid flooding the violations feed with system processes.
"""

from __future__ import annotations

import logging

log = logging.getLogger("gateway.process")


class ProcessMonitor:
    def __init__(self, policy: dict) -> None:
        self.policy = policy or {}
        # Optional explicit blocklist (names, lower-case). Absent by default.
        self.blocklist = {n.lower() for n in (self.policy.get("app_blocklist") or [])}
        self._psutil = None
        try:
            import psutil  # type: ignore

            self._psutil = psutil
        except Exception as exc:
            log.info("process monitor inactive (psutil unavailable): %s", exc)

    def poll(self) -> list[dict]:
        if self._psutil is None or not self.blocklist:
            return []
        out: list[dict] = []
        seen: set[str] = set()
        try:
            for proc in self._psutil.process_iter(["name"]):
                name = (proc.info.get("name") or "").lower()
                if name and name in self.blocklist and name not in seen:
                    seen.add(name)
                    out.append({
                        "type": "ROGUE_APP",
                        "severity": "warning",
                        "summary": f"Disallowed application running: {name}",
                        "detail": {"process": name},
                    })
        except Exception as exc:
            log.debug("process poll error: %s", exc)
        return out
