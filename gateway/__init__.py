"""MediaCore Studio Gateway — studio-resident playout agent.

Pull-based: leases playout commands from MediaERP, applies them to RadioBOSS
over localhost, confirms them in the playlist, and reports the outcome back.
The retry / backoff / DEAD brain lives server-side; this daemon is the hands.
"""

__version__ = "0.1.0"
