"""Logging setup — console + rotating file under data/."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(level: str = "INFO", db_path: str = "data/gateway.db") -> logging.Logger:
    log_dir = os.path.dirname(db_path) or "."
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("gateway")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "gateway.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
