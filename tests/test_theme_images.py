"""The stylesheet's small images (check marks, radio dots, arrows, tab close
buttons) live in a folder of this process's own, not under fixed names in
the shared temp folder, and a rule whose image is missing leaves the image
out instead of writing an invalid ``url()``."""

import os
import re
import tempfile

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture
def fresh_images(qapp, tmp_path, monkeypatch):
    """No image painted yet, and a system temp folder of the test's own."""
    from turboadb.gui import theme

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(theme, "_PNG_CACHE", {})
    monkeypatch.setattr(theme, "_PNG_DIR", None, raising=False)
    return theme


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def test_another_users_file_under_the_old_name_does_not_cost_the_check_mark(fresh_images,
                                                                           tmp_path):
    theme = fresh_images
    # On a shared /tmp a file another user left under the old fixed name can't
    # be replaced; a folder in its place makes the same write fail everywhere.
    (tmp_path / "turboadb-check-123456.png").mkdir()
    path = theme._checkmark_png("#123456")
    assert path and os.path.isfile(path)
    folder = os.path.dirname(path)
    assert _same(os.path.dirname(folder), tmp_path)  # a folder of its own in temp
    assert os.path.basename(folder).startswith("turboadb-qss-")
    if os.name != "nt":
        st = os.stat(folder)
        assert st.st_mode & 0o777 == 0o700 and st.st_uid == os.getuid()
    # painted once, then reused
    assert theme._checkmark_png("#123456") == path


def test_every_image_in_the_stylesheet_is_in_that_folder(fresh_images, tmp_path):
    theme = fresh_images
    images = re.findall(r"url\(([^)]*)\)", theme.stylesheet("dark"))
    assert images
    folders = {os.path.normcase(os.path.dirname(os.path.abspath(p))) for p in images}
    assert len(folders) == 1
    assert all(os.path.isfile(p) for p in images)
    assert not any(_same(os.path.dirname(p), tmp_path) for p in images)


def test_a_missing_image_leaves_every_rule_without_one(qapp, monkeypatch):
    from turboadb.gui import theme

    monkeypatch.setattr(theme, "_image", lambda _fn, _color: "")
    for name in theme.theme_names():
        css = theme.stylesheet(name)
        assert "url()" not in css, name
        # the rules themselves stay: a ticked box still shows its accent fill
        checked = re.search(r"QCheckBox::indicator:checked, QGroupBox::indicator:checked \{([^}]*)\}",
                            css)
        assert checked and "background:" in checked.group(1) and "image" not in checked.group(1)
        assert "QRadioButton::indicator:checked" in css
        assert "QMenu::indicator:checked" in css
