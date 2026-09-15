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
    python scripts/release.py 0.1.1 --rebuild-exe  # rebuild the exe even if dist/ has it
    python scripts/release.py 0.1.1 --no-exe     # lean package without the Windows exe

The wheel bundles the Windows GUI executable (turboadb/bin/turboadb-gui.exe). It
is copied from dist/TurboADB-<version>-win64.exe, which scripts/build_exe.py
builds first when dist/ has none for the release version.

The PyPI token is read from the environment, never hard-coded:
    TWINE_USERNAME=__token__   (default if unset)
    TWINE_PASSWORD=pypi-...    (your token)

PyPI permanently forbids re-uploading an existing version, so every release
must use a new version number.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "turboadb" / "__init__.py"

VERSION_RE = re.compile(r"^\s*\d+\.\d+\.\d+\s*$")
BUNDLED_EXE = "turboadb/bin/turboadb-gui.exe"


def run(cmd, **kw) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True, cwd=ROOT, **kw)


def release_exe(version: str) -> Path:
    """The versioned executable scripts/build_exe.py writes for *version*."""
    return ROOT / "dist" / f"TurboADB-{version}-win64.exe"


def bundle_exe(version: str, rebuild: bool = False) -> Path:
    """Copy this version's Windows executable into the package so the wheel
    ships it, building the executable first when dist/ has none (or *rebuild*)."""
    exe = release_exe(version)
    if rebuild or not exe.is_file():
        run([sys.executable, "scripts/build_exe.py"])
        if not exe.is_file():
            sys.exit(f"scripts/build_exe.py did not produce {exe.relative_to(ROOT)}")
    target = ROOT / BUNDLED_EXE
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exe, target)
    print(f"  bundled {exe.relative_to(ROOT)} -> {BUNDLED_EXE}")
    return target


def wheel_has_exe(wheel: Path) -> bool:
    import zipfile

    with zipfile.ZipFile(wheel) as zf:
        return BUNDLED_EXE in zf.namelist()


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
    new_text, n = re.subn(pattern, lambda m: m.group(0).replace(m.group(1), new), text, count=1)
    if n != 1:
        sys.exit(f"Could not update version in {label}")
    path.write_text(new_text, encoding="utf-8")
    print(f"  {label}: -> {new}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build and publish turboadb.")
    ap.add_argument("version", help="X.Y.Z, or patch/minor/major")
    ap.add_argument(
        "--dry-run", action="store_true", help="build and twine check only; do not upload"
    )
    ap.add_argument("--test-pypi", action="store_true", help="upload to TestPyPI instead of PyPI")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument(
        "--wheel-only",
        action="store_true",
        help="upload only the wheel (handy if sdist upload hangs)",
    )
    ap.add_argument(
        "--rebuild-exe",
        action="store_true",
        help="rebuild dist/TurboADB-<version>-win64.exe even if it already exists",
    )
    ap.add_argument(
        "--no-exe", action="store_true", help="build without the bundled Windows executable"
    )
    args = ap.parse_args(argv)

    cur = current_version()
    new = resolve_target(args.version, cur)
    print(f"Current version: {cur}\nNew version:     {new}")

    if not args.skip_tests:
        run([sys.executable, "tests/test_offline.py"])
        # the pytest suite (fake-adb unit tests) — skipped gracefully if pytest
        # isn't installed, so a minimal env can still cut a release
        try:
            import pytest  # noqa: F401

            run([sys.executable, "-m", "pytest", "tests/", "-q"])
        except ImportError:
            print(
                "  (pytest not installed — skipping the pytest suite; pip install pytest to run it)"
            )

    print("\nUpdating version strings:")
    # The version must be bumped before building (the wheel embeds it), but a
    # failed build/check must not leave the files bumped for a release that
    # never happened — the next `patch` run would then skip a version number.
    originals = {path: path.read_text(encoding="utf-8") for path in (PYPROJECT, INIT)}
    set_version(PYPROJECT, r'(?m)^version\s*=\s*"([^"]+)"', new, "pyproject.toml")
    set_version(INIT, r'__version__\s*=\s*"([^"]+)"', new, "turboadb/__init__.py")

    try:
        if (ROOT / "build").exists():
            shutil.rmtree(ROOT / "build")
        for egg in ROOT.glob("*.egg-info"):
            shutil.rmtree(egg)
        # old packages only: dist/TurboADB-<version>-win64.exe is the release exe
        for old in [*(ROOT / "dist").glob("*.whl"), *(ROOT / "dist").glob("*.tar.gz")]:
            old.unlink()

        if args.no_exe:
            (ROOT / BUNDLED_EXE).unlink(missing_ok=True)
        else:
            bundle_exe(new, rebuild=args.rebuild_exe)

        run([sys.executable, "-m", "build"])
        artifacts = sorted((ROOT / "dist").glob("*.whl")) + sorted(
            (ROOT / "dist").glob("*.tar.gz")
        )
        if not artifacts:
            sys.exit("Build completed without producing a wheel or source archive.")
        if not args.no_exe:
            for wheel in (p for p in artifacts if p.suffix == ".whl"):
                if not wheel_has_exe(wheel):
                    sys.exit(f"{wheel.name} is missing {BUNDLED_EXE}")
        run([sys.executable, "-m", "twine", "check", *(str(p) for p in artifacts)])
    except BaseException:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")
        print(f"\nBuild/check failed — version strings restored to {cur}.", file=sys.stderr)
        raise

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
        cmd += [str(p) for p in sorted((ROOT / "dist").glob("*.whl"))]
    else:
        cmd += [str(p) for p in artifacts]
    run(cmd)

    target = "TestPyPI" if args.test_pypi else "PyPI"
    print(f"\nDone. Published turboadb {new} to {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
