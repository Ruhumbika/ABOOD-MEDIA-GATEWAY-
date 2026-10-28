"""Entrypoint: `python -m gateway` (or the mediacore-gateway console script)."""

from __future__ import annotations

import logging
import signal

from .config import load_settings
from .log import setup_logging
from .worker import Worker


def main() -> int:
    settings = load_settings()
    setup_logging(settings.log_level, settings.db_path)
    log = logging.getLogger("gateway")

    worker = Worker(settings)

    def _handle(signum, _frame):
        log.info("signal %s received — shutting down", signum)
        worker.stop()

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, _handle)
            except (ValueError, OSError, AttributeError):
                pass

    try:
        worker.run()
    except KeyboardInterrupt:
        worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
