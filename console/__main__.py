"""Entrypoint for the operator console: `python -m console` (or the built exe)."""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from .main_window import MainWindow


def _set_windows_app_id() -> None:
    """Give Windows a distinct AppUserModelID so the taskbar/Start-Menu use OUR
    (OraMedia) icon and group correctly — instead of Python's or a stale cached
    icon from an earlier build."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Oraculyn.MediaCore.StudioGateway.Console"
        )
    except Exception:
        pass


def main() -> int:
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("MediaCore Studio Gateway Console")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
