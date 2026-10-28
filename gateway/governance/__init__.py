"""Host governance (Phase 6) — Windows OS-level enforcement assists.

These modules detect and (where possible) apply host policy: USB storage,
running apps, folder ACLs. They are DEFENSIVE by design — every OS binding is
imported lazily and guarded, so the daemon runs cross-platform (a no-op off
Windows / without pywin32/psutil/wmi) and the durable enforcement backstop
remains Windows GPO / AppLocker / WDAC.
"""
