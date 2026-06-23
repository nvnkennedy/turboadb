#!/usr/bin/env python
"""
Build the TurboADB PyQt5 GUI into a standalone Windows executable with the
bundled automotive/Android icon, using PyInstaller.

    pip install "turboadb[gui]" pyinstaller
    python scripts/make_icon.py          # (re)generate the icon first
    python scripts/build_exe.py          # one-file build via the spec

Output:
    dist/turboadb-gui.exe

Run from the repo root so the spec finds turboadb/.
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "turboadb-gui.spec"


def main(argv=None) -> int:
    try:
        import PyInstaller  # noqa
    except ImportError:
        sys.exit("PyInstaller is required: pip install pyinstaller")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)]
    print("Running:", " ".join(str(c) for c in cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc == 0:
        print("\nDone. Executable at: dist/turboadb-gui.exe")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
