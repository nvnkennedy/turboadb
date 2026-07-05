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
    m = re.search(r'(?m)^\s*license\s*=\s*(.+)$', text)
    assert m, "no `license =` line in pyproject.toml"
    value = m.group(1).strip()
    assert value.startswith("{"), (
        f"license must be the table form `{{ text = \"MIT\" }}`, not the SPDX "
        f"string (that needs setuptools>=77, absent on Python 3.8). Got: {value}")
    assert "text" in value and "MIT" in value, value


def test_version_strings_match():
    """pyproject version must equal turboadb.__version__ (release.py keeps them
    in lockstep — a mismatch means a bad manual edit)."""
    import turboadb
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', _read_pyproject())
    assert m, "no version in pyproject.toml"
    assert m.group(1) == turboadb.__version__, (m.group(1), turboadb.__version__)


def test_requires_python_declared():
    assert re.search(r'(?m)^requires-python\s*=', _read_pyproject()), \
        "requires-python must stay declared"
