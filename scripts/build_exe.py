#!/usr/bin/env python
"""
Build the TurboADB PyQt5 GUI into a standalone Windows executable with the
bundled automotive/Android icon, using PyInstaller.

    pip install "turboadb[gui]" pyinstaller
    python scripts/make_icon.py          # (re)generate the icon first
    python scripts/build_exe.py          # one-file build via the spec

Output:
    dist/TurboADB-<version>-win64.exe

Run from the repo root so the spec finds turboadb/.
"""

from __future__ import annotations

import sys
import subprocess
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "turboadb-gui.spec"


def executable_path() -> Path:
    """Return the versioned executable path declared by the PyInstaller spec."""
    text = (ROOT / "turboadb" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match is None:
        raise RuntimeError("Could not read TurboADB's version.")
    return ROOT / "dist" / f"TurboADB-{match.group(1)}-win64.exe"


def main(argv=None) -> int:
    try:
        import PyInstaller  # noqa
    except ImportError:
        sys.exit("PyInstaller is required: pip install pyinstaller")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)]
    print("Running:", " ".join(str(c) for c in cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc == 0:
        print(f"\nDone. Executable at: {executable_path().relative_to(ROOT)}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
