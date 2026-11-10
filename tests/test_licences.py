"""What the app ships is named, with its licence, and the licence texts travel with it.

TurboADB's own code is MIT, but the Windows app contains PyQt5 (GPL v3) and Qt
(LGPL v3). The exe, the wheel and the source package all carry those texts.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LICENSES = os.path.join(ROOT, "turboadb", "licenses")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("name, heading", [
    ("GPL-3.0.txt", "GNU GENERAL PUBLIC LICENSE"),
    ("LGPL-3.0.txt", "GNU LESSER GENERAL PUBLIC LICENSE"),
])
def test_the_licence_texts_are_there_in_full(name, heading):
    with open(os.path.join(LICENSES, name), encoding="utf-8") as fh:
        text = fh.read()
    assert heading in text[:200] and "Version 3, 29 June 2007" in text[:300]
    assert len(text) > 7000


def test_the_notices_name_the_copyleft_parts():
    notices = _read("turboadb", "licenses", "THIRD-PARTY-NOTICES.md")
    assert re.search(r"PyQt5.*\| GPL v3", notices)
    assert re.search(r"Qt 5\.15.*\| LGPL v3", notices)
    assert "distributed under the GPL v3" in notices


@pytest.mark.parametrize("package", [
    "pywinrm", "requests", "requests_ntlm", "pyspnego", "xmltodict", "keyring", "cryptography",
])
def test_the_notices_list_what_the_app_is_built_with(package):
    assert package in _read("turboadb", "licenses", "THIRD-PARTY-NOTICES.md")


def test_the_notices_do_not_claim_adb_or_scrcpy_are_shipped():
    notices = _read("turboadb", "licenses", "THIRD-PARTY-NOTICES.md")
    assert "does not include adb or scrcpy" in notices


@pytest.mark.parametrize("parts, needle", [
    (("turboadb-gui.spec",), "turboadb/licenses"),
    (("pyproject.toml",), '"licenses/*"'),
    (("MANIFEST.in",), "turboadb/licenses"),
])
def test_every_package_carries_the_licences(parts, needle):
    assert needle in _read(*parts)


def test_the_readme_says_what_the_app_is_licensed_under():
    readme = _read("README.md")
    section = readme[readme.index("## License"):]
    assert "GPL v3" in section and "MIT License" in section
    assert "includes the official" not in section  # nothing of adb or scrcpy is shipped


@pytest.mark.parametrize("name", ["q-a.yml", "ideas.yml", "show-and-tell.yml"])
def test_discussion_forms_are_named_after_their_categories(name):
    """GitHub finds a form by the category's slug: any other name is ignored."""
    text = _read(".github", "DISCUSSION_TEMPLATE", name)
    assert text.startswith("body:\n") or text.startswith("body:\r\n")
    assert "required: true" in text


def test_the_issue_chooser_points_to_discussions():
    text = _read(".github", "ISSUE_TEMPLATE", "config.yml")
    assert "discussions/categories/q-a" in text and "discussions/categories/ideas" in text
