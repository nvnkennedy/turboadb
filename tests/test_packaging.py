"""Guard the packaging metadata so it stays installable on every supported
setuptools — the `license = "MIT"` SPDX string form silently worked on new
setuptools but broke the editable/source install on the Python 3.8 CI (whose
setuptools predates PEP 639).

Deliberately dependency-free: parsed as text, NOT via setuptools/tomllib — the
setuptools that ships with Python 3.8 (56.x) has no ``setuptools.config`` module
and no ``tomllib`` exists before 3.11, so importing either here would itself
fail on 3.8, which is the very platform this test protects."""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(ROOT, "pyproject.toml")


def _read_pyproject():
    with open(PYPROJECT, encoding="utf-8") as fh:
        return fh.read()


def test_license_uses_portable_table_form():
    """`license` must be the classic table form ``{ text = "MIT" }``.

    The bare SPDX string ``license = "MIT"`` only validates on setuptools>=77,
    which isn't available on Python 3.8 — so it broke source/editable installs
    (and the 3.8 CI) there. This asserts the portable form so it can't regress."""
    text = _read_pyproject()
    m = re.search(r"(?m)^\s*license\s*=\s*(.+)$", text)
    assert m, "no `license =` line in pyproject.toml"
    value = m.group(1).strip()
    assert value.startswith("{"), (
        f'license must be the table form `{{ text = "MIT" }}`, not the SPDX '
        f"string (that needs setuptools>=77, absent on Python 3.8). Got: {value}"
    )
    assert "text" in value and "MIT" in value, value


def test_version_strings_match():
    """pyproject version must equal turboadb.__version__ (release.py keeps them
    in lockstep — a mismatch means a bad manual edit)."""
    import turboadb

    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', _read_pyproject())
    assert m, "no version in pyproject.toml"
    assert m.group(1) == turboadb.__version__, (m.group(1), turboadb.__version__)


def test_requires_python_declared():
    assert re.search(r"(?m)^requires-python\s*=", _read_pyproject()), (
        "requires-python must stay declared"
    )


# --------------------------------------------------------------------------- #
# the GitHub Release page carries the version's changelog section
# --------------------------------------------------------------------------- #
def _release_notes_module():
    import importlib.util

    path = os.path.join(ROOT, "scripts", "release_notes.py")
    spec = importlib.util.spec_from_file_location("release_notes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SAMPLE = """# Changelog

Intro text.

## 3.0.0

### New

- **Big thing.** It does a lot.

### Fixed

- A small thing.

## 2.9.1

- Only a fix.

## 2.9.0

- The first one.
"""


def test_the_current_version_has_release_notes():
    import turboadb

    rn = _release_notes_module()
    notes = rn.release_notes(turboadb.__version__)
    assert "\n### " not in notes and notes.startswith("## ")
    assert f"TurboADB-{turboadb.__version__}-win64.exe" in notes
    assert "pip install --upgrade turboadb" in notes
    assert f"...v{turboadb.__version__}" in notes  # the compare link


def test_release_notes_take_exactly_one_section():
    rn = _release_notes_module()
    notes = rn.release_notes("v3.0.0", _SAMPLE)
    assert notes.startswith("## New\n\n- **Big thing.**")
    assert "## Fixed\n\n- A small thing." in notes
    assert "Only a fix" not in notes and "Intro text" not in notes
    assert notes.endswith("compare/v2.9.1...v3.0.0\n")
    middle = rn.release_notes("2.9.1", _SAMPLE)
    assert middle.startswith("- Only a fix.") and "v2.9.0...v2.9.1" in middle
    assert "compare" not in rn.release_notes("2.9.0", _SAMPLE)  # the oldest entry


def test_a_version_without_notes_is_refused():
    import pytest

    rn = _release_notes_module()
    with pytest.raises(ValueError, match="no notes for 9.9.9"):
        rn.release_notes("9.9.9", _SAMPLE)
    with pytest.raises(ValueError):
        rn.release_notes("3.0.0", "## 3.0.0\n\n## 2.9.0\n\n- x\n")  # empty section
    with pytest.raises(SystemExit) as exc:
        rn.main(["9.9.9"])
    assert "no notes for 9.9.9" in str(exc.value)


def test_release_notes_are_written_as_utf8(tmp_path):
    rn = _release_notes_module()
    out = tmp_path / "notes.md"
    assert rn.main(["--output", str(out)]) == 0
    data = out.read_bytes()
    assert b"\r\n" not in data
    import turboadb

    assert data.decode("utf-8") == rn.release_notes(turboadb.__version__)


def test_the_release_workflow_publishes_the_notes():
    with open(os.path.join(ROOT, ".github", "workflows", "release.yml"), encoding="utf-8") as fh:
        workflow = fh.read()
    assert "python scripts/release_notes.py --output release-notes.md" in workflow
    assert "body_path: release-notes.md" in workflow
    # written before the long build, so missing notes fail fast
    assert workflow.index("release_notes.py") < workflow.index("Build the one-file GUI exe")
