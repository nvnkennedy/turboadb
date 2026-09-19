"""The PyPI wheel ships the Windows GUI executable: `turboadb-gui` starts it
when PyQt5 isn't installed, and scripts/release.py bundles it and refuses a
wheel without it."""
import importlib.util
import os
import sys
import zipfile

import pytest

import turboadb.cli as cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _release_module():
    spec = importlib.util.spec_from_file_location(
        "turboadb_release_script", os.path.join(ROOT, "scripts", "release.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_data_ships_the_bundled_exe():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        text = fh.read()
    package_data = text.split("[tool.setuptools.package-data]", 1)[1].split("\n[", 1)[0]
    assert '"bin/*.exe"' in package_data


@pytest.mark.skipif(os.name != "nt", reason="the bundled executable is Windows-only")
def test_without_pyqt5_the_bundled_exe_is_started(tmp_path, monkeypatch):
    exe = tmp_path / "turboadb-gui.exe"
    exe.write_bytes(b"MZ")
    calls = []
    monkeypatch.setitem(sys.modules, "PyQt5", None)  # import PyQt5 -> ImportError
    monkeypatch.setattr(cli, "_gui_exe_path", lambda: str(exe))
    monkeypatch.setattr(cli, "_staged_exe", lambda src: src)
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(cli, "_prewarm_gui_adb_server", lambda: calls.append("prewarm"))
    assert cli.launch_gui(["--flag"]) == 0
    assert calls == [[str(exe), "--flag"]]


def test_without_pyqt5_or_exe_the_install_hint_is_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "PyQt5", None)
    monkeypatch.setattr(cli, "_gui_exe_path", lambda: str(tmp_path / "missing.exe"))
    monkeypatch.setattr(cli, "_prewarm_gui_adb_server", lambda: None)
    assert cli.launch_gui([]) == 1
    assert 'pip install "turboadb[gui]"' in capsys.readouterr().err


def test_staged_exe_runs_a_temp_copy(tmp_path, monkeypatch):
    import tempfile

    src = tmp_path / "turboadb-gui.exe"
    src.write_bytes(b"MZ" * 10)
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    staged = cli._staged_exe(str(src))
    assert os.path.dirname(staged) == str(temp)
    assert open(staged, "rb").read() == src.read_bytes()
    assert cli._staged_exe(str(src)) == staged


def test_release_bundles_the_versioned_exe(tmp_path, monkeypatch):
    release = _release_module()
    monkeypatch.setattr(release, "ROOT", tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "TurboADB-9.9.9-win64.exe").write_bytes(b"MZ-9.9.9")
    monkeypatch.setattr(release, "run", lambda cmd: pytest.fail(f"unexpected build: {cmd}"))
    target = release.bundle_exe("9.9.9")
    assert target == tmp_path / "turboadb" / "bin" / "turboadb-gui.exe"
    assert target.read_bytes() == b"MZ-9.9.9"


def test_release_detects_a_wheel_without_the_exe(tmp_path):
    release = _release_module()
    good = tmp_path / "good.whl"
    bad = tmp_path / "bad.whl"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("turboadb/__init__.py", "")
        zf.writestr("turboadb/bin/turboadb-gui.exe", "MZ")
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("turboadb/__init__.py", "")
    assert release.wheel_has_exe(good)
    assert not release.wheel_has_exe(bad)


def _gui_entry_module():
    spec = importlib.util.spec_from_file_location(
        "turboadb_gui_entry_script", os.path.join(ROOT, "scripts", "gui_entry.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exe_self_test_finishes_without_a_stderr(tmp_path, monkeypatch):
    """A windowed exe has sys.stderr = None: writing the verdict there raised,
    and the frozen exe then waited on an error dialog until CI cancelled it."""
    import types

    entry = _gui_entry_module()
    fake_winrm = types.ModuleType("winrm")
    fake_winrm.Session = lambda *args, **kwargs: object()
    monkeypatch.setitem(sys.modules, "winrm", fake_winrm)
    monkeypatch.setattr(importlib, "import_module", lambda name: types.ModuleType(name))
    monkeypatch.setattr(os.path, "expanduser", lambda path: str(tmp_path))
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setenv("TURBOADB_SELFTEST", "winrm")
    assert entry.run() == 0
    report = (tmp_path / ".turboadb" / "winrm-selftest.txt").read_text(encoding="utf-8")
    assert report.startswith("WINRM-SELFTEST: ALL OK")


def test_exe_startup_failure_is_reported_without_a_stderr(tmp_path, monkeypatch):
    entry = _gui_entry_module()
    monkeypatch.setattr(os.path, "expanduser", lambda path: str(tmp_path))
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setitem(sys.modules, "ctypes", None)  # no message box either
    entry._fatal("boom")  # must not raise
    assert "boom" in (tmp_path / ".turboadb" / "crash.log").read_text(encoding="utf-8")
