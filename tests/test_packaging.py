"""Guard the packaging metadata so it stays installable on every supported
setuptools — the `license = "MIT"` SPDX string form silently worked on new
setuptools but broke the editable/source install on the Python 3.8 CI (whose
setuptools predates PEP 639)."""

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(ROOT, "pyproject.toml")


def test_pyproject_validates_on_this_setuptools():
    """setuptools must accept our [project] metadata as-is. On an older
    setuptools (like the 3.8 CI's) the SPDX license string fails here; the
    classic ``license = {text = ...}`` table form passes everywhere."""
    pytest.importorskip("setuptools")
    from setuptools.config.pyprojecttoml import read_configuration
    cfg = read_configuration(PYPROJECT, ignore_option_errors=False)
    lic = cfg["project"]["license"]
    # accept either the table form (older setuptools) or a normalized string
    assert lic in ("MIT", {"text": "MIT"}) or lic.get("text") == "MIT", lic


def test_version_strings_match():
    """pyproject version must equal turboadb.__version__ (release.py keeps them
    in lockstep — a mismatch means a bad manual edit)."""
    import re
    import turboadb
    text = open(PYPROJECT, encoding="utf-8").read()
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert m, "no version in pyproject.toml"
    assert m.group(1) == turboadb.__version__, (m.group(1), turboadb.__version__)
