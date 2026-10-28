"""PyInstaller entrypoint — bundles the gateway worker into a single .exe.

Built with `packaging/build.ps1` into `dist/mediacore-gateway.exe`, which runs
the same worker loop as `python -m gateway` but needs no Python on the studio PC.
"""

import sys

from gateway.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
