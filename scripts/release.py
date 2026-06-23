#!/usr/bin/env python
"""
Release helper for turboadb:  bump version -> test -> build -> check -> upload.

Usage
-----
    python scripts/release.py 0.1.1              # set an explicit version
    python scripts/release.py patch              # 0.1.0 -> 0.1.1
    python scripts/release.py minor              # 0.1.0 -> 0.2.0
    python scripts/release.py major              # 0.1.0 -> 1.0.0
    python scripts/release.py 0.1.1 --dry-run    # build + check, do NOT upload
    python scripts/release.py 0.1.1 --test-pypi  # upload to TestPyPI
    python scripts/release.py 0.1.1 --wheel-only # upload only the wheel (skip sdist)

The PyPI token is read from the environment, never hard-coded:
    TWINE_USERNAME=__token__   (default if unset)
    TWINE_PASSWORD=pypi-...    (your token)

PyPI permanently forbids re-uploading an existing version, so every release
must use a new version number.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "turboadb" / "__init__.py"

VERSION_RE = re.compile(r"^\s*\d+\.\d+\.\d+\s*$")


def run(cmd, **kw) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True, cwd=ROOT, **kw)


def current_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not m:
        sys.exit("Could not find version in pyproject.toml")
    return m.group(1)


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(part)


def resolve_target(arg: str, cur: str) -> str:
    if arg in ("patch", "minor", "major"):
        return bump(cur, arg)
    if VERSION_RE.match(arg):
        return arg.strip()
    sys.exit(f"Invalid version/part: {arg!r} (use X.Y.Z or patch/minor/major)")


def set_version(path: Path, pattern: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, n = re.subn(pattern, lambda m: m.group(0).replace(m.group(1), new),
                          text, count=1)
    if n != 1:
        sys.exit(f"Could not update version in {label}")
    path.write_text(new_text, encoding="utf-8")
    print(f"  {label}: -> {new}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build and publish turboadb.")
    ap.add_argument("version", help="X.Y.Z, or patch/minor/major")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and twine check only; do not upload")
    ap.add_argument("--test-pypi", action="store_true",
                    help="upload to TestPyPI instead of PyPI")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--wheel-only", action="store_true",
                    help="upload only the wheel (handy if sdist upload hangs)")
    args = ap.parse_args(argv)

    cur = current_version()
    new = resolve_target(args.version, cur)
    print(f"Current version: {cur}\nNew version:     {new}")

    if not args.skip_tests:
        run([sys.executable, "tests/test_offline.py"])

    print("\nUpdating version strings:")
    set_version(PYPROJECT, r'(?m)^version\s*=\s*"([^"]+)"', new, "pyproject.toml")
    set_version(INIT, r'__version__\s*=\s*"([^"]+)"', new, "turboadb/__init__.py")

    for d in ("dist", "build"):
        p = ROOT / d
        if p.exists():
            for f in sorted(p.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
            p.rmdir()
    for egg in ROOT.glob("*.egg-info"):
        for f in sorted(egg.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        egg.rmdir()

    run([sys.executable, "-m", "build"])
    run([sys.executable, "-m", "twine", "check", "dist/*"])

    if args.dry_run:
        print("\n--dry-run: built and validated, skipping upload.")
        return 0

    if "TWINE_PASSWORD" not in os.environ:
        sys.exit("Set TWINE_PASSWORD (your PyPI token) before uploading.")
    os.environ.setdefault("TWINE_USERNAME", "__token__")
    cmd = [sys.executable, "-m", "twine", "upload"]
    if args.test_pypi:
        cmd += ["--repository", "testpypi"]
    if args.wheel_only:
        cmd += sorted(glob.glob(str(ROOT / "dist" / "*.whl")))
    else:
        cmd += ["dist/*"]
    run(cmd)

    target = "TestPyPI" if args.test_pypi else "PyPI"
    print(f"\nDone. Published turboadb {new} to {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
