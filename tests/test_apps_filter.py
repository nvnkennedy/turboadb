"""The Apps page's package filter ignores spaces around what is typed.

A name pasted with a space before or after it (" chrome") matched nothing and
the page said no package matched, while the "No packages match" message
itself showed the text trimmed.
"""
import pytest

pytest.importorskip("PyQt5")

from turboadb.results import OperationResult  # noqa: E402

_PACKAGES = ["com.android.chrome", "com.example.app", "com.google.android.apps.maps"]


class _Device:
    def list_packages(self, *_a, **_k):
        return OperationResult(True, "list_packages", value=list(_PACKAGES))


@pytest.fixture
def apps(qapp, monkeypatch):
    from turboadb.gui import apps_panel as ap

    def run(jobs, fn, on_done=None, on_fail=None):
        try:
            result = fn()
        except Exception as exc:
            if on_fail is not None:
                on_fail(f"{type(exc).__name__}: {exc}")
            return None
        if on_done is not None:
            on_done(result)
        return None

    monkeypatch.setattr(ap, "run_job", run)
    panel = ap.AppsPanel(_Device())
    try:
        panel.refresh()
        yield panel
    finally:
        panel.close_panel()


def _shown(panel):
    return [panel.list.item(i).text() for i in range(panel.list.count())]


@pytest.mark.parametrize("typed", ["chrome", " chrome", "chrome  ", "\tChrome "])
def test_spaces_around_the_filter_do_not_count(apps, typed):
    apps.filt.setText(typed)
    assert _shown(apps) == ["com.android.chrome"]


def test_an_empty_or_blank_filter_shows_every_package(apps):
    apps.filt.setText("   ")
    assert _shown(apps) == _PACKAGES
