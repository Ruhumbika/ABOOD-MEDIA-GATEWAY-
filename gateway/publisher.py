"""Schedule publication (Phase 4).

Fetches the approved day's schedule from MediaERP, syncs every creative to the
studio disk (FileSync), and writes a local M3U playlist with LOCAL paths. That
file is the resilience backstop: a full day of approved content can air even if
the uplink to MediaERP is down all day.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("gateway.publisher")


class SchedulePublisher:
    def __init__(self, erp, filesync, schedule_folder: str) -> None:
        self.erp = erp
        self.filesync = filesync
        self.schedule_folder = schedule_folder
        os.makedirs(schedule_folder, exist_ok=True)

    def publish(self, date: str) -> dict:
        sched = self.erp.get_schedule(date)  # raises ErpError if the uplink is down
        items = sched.get("items", [])

        lines = ["#EXTM3U"]
        published = 0
        missing = 0
        for it in items:
            local = self.filesync.ensure(it) if it.get("asset_id") else None
            local = local or it.get("file")
            if not local:
                missing += 1
                log.warning("schedule item has no local file (asset %s)", it.get("asset_id"))
                continue
            duration = it.get("duration") or -1
            title = it.get("title") or ""
            lines.append(f"#EXTINF:{duration},{title}")
            lines.append(local)
            published += 1

        path = os.path.join(self.schedule_folder, f"{date}.m3u")
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, path)  # atomic

        log.info("published %s schedule: %d item(s), %d missing → %s", date, published, missing, path)
        return {"path": path, "published": published, "missing": missing}
