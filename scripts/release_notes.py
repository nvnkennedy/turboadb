#!/usr/bin/env python
"""
Write one version's CHANGELOG.md section as GitHub release notes.

    python scripts/release_notes.py                  # the version in turboadb/__init__.py
    python scripts/release_notes.py v2.5.0           # or a given version / tag
    python scripts/release_notes.py --output notes.md

The Release workflow passes the result to the GitHub Release, so the release
page carries the same notes as the changelog instead of a bare list of files.
A version with no changelog section is an error: a release should not go out
without saying what changed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
INIT = ROOT / "turboadb" / "__init__.py"
REPO = "https://github.com/NVNKENNEDY/turboadb"

_HEADING = re.compile(r"(?m)^## +v?(\d+\.\d+\.\d+)\s*$")


def current_version() -> str:
    """The version in turboadb/__init__.py (read as text, no import)."""
    match = re.search(r'__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8"))
    if match is None:
        sys.exit("Could not read __version__ from turboadb/__init__.py")
    return match.group(1)


def changelog_section(text: str, version: str):
    """*version*'s section of the changelog *text* and the version before it.

    Returns ``(body, previous)``; *previous* is None for the oldest entry.
    Raises ValueError when the changelog has no section for *version*."""
    headings = list(_HEADING.finditer(text))
    for i, heading in enumerate(headings):
        if heading.group(1) != version:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[heading.end():end].strip()
        if not body:
            break
        previous = headings[i + 1].group(1) if i + 1 < len(headings) else None
        return body, previous
    raise ValueError(f"CHANGELOG.md has no notes for {version}")


def release_notes(version: str, text: str | None = None) -> str:
    """The GitHub release notes for *version* (``v`` prefix allowed)."""
    version = version.strip().lstrip("v")
    if text is None:
        text = CHANGELOG.read_text(encoding="utf-8")
    body, previous = changelog_section(text, version)
    # the release page has its own title, so the changelog's ### sections
    # become the page's top-level headings
    body = re.sub(r"(?m)^### ", "## ", body)
    lines = [
        body,
        "",
        "## Install",
        "",
        f"- **Windows app:** download `TurboADB-{version}-win64.exe` below and run it.",
        "- **pip:** `pip install --upgrade turboadb`",
    ]
    if previous:
        lines += ["", f"**Full changes:** {REPO}/compare/v{previous}...v{version}"]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Print a version's release notes.")
    ap.add_argument("version", nargs="?", help="X.Y.Z or vX.Y.Z (default: turboadb's version)")
    ap.add_argument("--output", help="write the notes to this file (UTF-8) instead of stdout")
    args = ap.parse_args(argv)
    try:
        notes = release_notes(args.version or current_version())
    except ValueError as exc:
        sys.exit(str(exc))
    if args.output:
        # UTF-8 whatever the console's code page (the notes use · and ▾)
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(notes)
    elif hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(notes.encode("utf-8"))
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
