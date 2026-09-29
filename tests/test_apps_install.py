"""Installing APKs from the Apps tab: only files adb can install are offered,
app bundles are refused with the reason, and several APKs are installed as
separate apps or as the parts of one app, as the user says.

Headless (Qt offscreen); the device is a fake that records each install."""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt5")

from turboadb.results import OperationResult  # noqa: E402


class _Device:
    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.installs = []

    def list_packages(self, *_a, **_k):
        return OperationResult(True, "list_packages", value=["com.example.app"])

    def install(self, path, **_k):
        self.installs.append(("install", path))
        if path in self.refuse:
            return OperationResult(False, "install", error=RuntimeError(
                "Install failed: Failure [INSTALL_FAILED_INVALID_APK]"))
        return OperationResult(True, "install", value="Success")

    def install_multiple(self, paths, **_k):
        self.installs.append(("install-multiple", list(paths)))
        return OperationResult(True, "install_multiple", value="Success")


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
    warnings = []
    monkeypatch.setattr(ap.QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a)))
    picked = {"files": [], "filter": None}

    def pick(*_args, **kwargs):
        picked["filter"] = kwargs.get("filter")
        return list(picked["files"]), ""

    monkeypatch.setattr(ap.QFileDialog, "getOpenFileNames", staticmethod(pick))
    device = _Device(refuse={"C:/apks/broken.apk"})
    panel = ap.AppsPanel(device)
    logs = []
    panel.log.connect(logs.append)
    panel.device, panel.picked, panel.warnings, panel.logs = device, picked, warnings, logs
    try:
        yield panel
    finally:
        panel.close_panel()


def _install(panel, monkeypatch, files, splits=None):
    panel.picked["files"] = files
    monkeypatch.setattr(panel, "_ask_splits", lambda _files: splits)
    panel._install()
    return panel.device.installs


def test_the_file_dialog_offers_what_adb_installs(apps, monkeypatch):
    """.apks and .apkm were offered as supported, and adb then refused them."""
    _install(apps, monkeypatch, [])
    assert "*.apk)" in apps.picked["filter"]
    assert "*.apks" not in apps.picked["filter"] and "*.apkm" not in apps.picked["filter"]


def test_an_app_bundle_is_refused_with_the_reason(apps, monkeypatch):
    installs = _install(apps, monkeypatch, ["C:/apks/app.apks", "C:/apks/a.apk"])
    assert installs == []
    assert len(apps.warnings) == 1 and "app.apks" in apps.warnings[0][2]


def test_separate_apps_are_installed_one_after_another(apps, monkeypatch):
    """install-multiple put two different apps into one package: both failed."""
    installs = _install(apps, monkeypatch, ["C:/apks/a.apk", "C:/apks/b.apk"], splits=False)
    assert installs == [("install", "C:/apks/a.apk"), ("install", "C:/apks/b.apk")]
    assert any(line.startswith("[OK] install a.apk, b.apk") for line in apps.logs)


def test_one_app_that_fails_does_not_stop_the_others(apps, monkeypatch):
    files = ["C:/apks/a.apk", "C:/apks/broken.apk", "C:/apks/c.apk"]
    installs = _install(apps, monkeypatch, files, splits=False)
    assert installs == [("install", path) for path in files]
    [error] = [line for line in apps.logs if line.startswith("[ERROR]")]
    assert "2 of 3 installed" in error and "broken.apk" in error


def test_the_parts_of_one_app_are_installed_together(apps, monkeypatch):
    files = ["C:/apks/base.apk", "C:/apks/split_config.arm64_v8a.apk"]
    assert _install(apps, monkeypatch, files, splits=True) == [("install-multiple", files)]


def test_cancelling_the_question_installs_nothing(apps, monkeypatch):
    assert _install(apps, monkeypatch, ["C:/apks/a.apk", "C:/apks/b.apk"], splits=None) == []


def test_split_names_make_parts_of_one_app_the_default(apps, monkeypatch):
    from turboadb.gui import apps_panel as ap

    boxes = []
    monkeypatch.setattr(ap.QMessageBox, "exec_", lambda box: boxes.append(box) or 0)
    assert apps._ask_splits(["C:/x/base.apk", "C:/x/split_config.en.apk"]) is None
    assert apps._ask_splits(["C:/x/maps.apk", "C:/x/music.apk"]) is None
    assert [box.defaultButton().text() for box in boxes] == ["Parts of one app",
                                                             "Separate apps"]
