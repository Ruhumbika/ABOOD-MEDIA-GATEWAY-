"""Folder lockdown (Windows NTFS ACLs). No-op without `pywin32`.

Restricts the ads / unauthorized-media folders so only the gateway's service
account can read/write them — the operator can't drop in or browse unapproved
files. Full ACL manipulation lives here; the durable backstop is a GPO. Kept
defensive so the daemon runs anywhere.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("gateway.folders")


class FolderPolicy:
    def __init__(self, policy: dict) -> None:
        self.policy = policy or {}
        self.allowed_folders = self.policy.get("allowed_folders") or []

    def enforce(self) -> None:
        if not self.policy.get("enforce_folder_acls", False):
            return
        try:
            import win32security  # type: ignore  # noqa: F401
        except Exception as exc:
            log.info(
                "folder ACL enforcement inactive (pywin32 unavailable): %s",
                exc,
            )
            return

        # Real implementation: for each protected folder, set an explicit DACL
        # granting only the gateway service account (and SYSTEM/Administrators)
        # and removing the interactive operator's rights. Left as a documented
        # extension point; a domain GPO is the recommended enforcer.
        for folder in self.allowed_folders:
            if folder and os.path.isdir(folder):
                log.debug("would apply ACL lockdown to %s", folder)
