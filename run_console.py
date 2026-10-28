"""PyInstaller entrypoint for the operator console (windowed .exe).

Built by packaging/build.ps1 into dist\mediacore-gateway-console.exe — the same
window as `python -m console`, but self-contained (no Python on the studio PC).
"""

import sys

from console.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
