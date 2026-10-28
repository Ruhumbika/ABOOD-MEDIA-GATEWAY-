"""MediaCore Studio Gateway — operator console (read-only, RadiQ-style).

A desktop window that mirrors the legacy RadiQ layout but is backed by the NEW
gateway's live data: RadioBOSS playback + playlist, the local work/ack queue,
the gateway log, and the Windows service state. It runs ALONGSIDE the headless
gateway service (which keeps doing delivery + governance) and does not control
playout — so the compliance model is preserved.
"""

__version__ = "0.1.0"
