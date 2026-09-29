"""scripts/release.py ships the exe that was built and tested (the version bump
no longer makes it look stale), and stops before anything runs when
CHANGELOG.md has no notes for the new version."""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _release():
    spec = importlib.util.spec_from_file_location(
        "turboadb_release_script_under_test", os.path.join(ROOT, "scripts", "release.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A release tree at 1.2.3 whose sources predate dist/'s 1.2.4 exe, and a
    fake ``run`` that records commands and fakes `python -m build`."""
    release = _release()
    (tmp_path / "turboadb").mkdir()
    (tmp_path / "dist").mkdir()
    pyproject = tmp_path / "pyproject.toml"
    init = tmp_path / "turboadb" / "__init__.py"
    pyproject.write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    init.write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("## 1.2.4\n\n- A fix.\n\n## 1.2.3\n\n- Older.\n",
                                           encoding="utf-8")
    exe = tmp_path / "dist" / "TurboADB-1.2.4-win64.exe"
    exe.write_bytes(b"MZ-tested")
    for path in (pyproject, init):
        os.utime(path, (1_000_000, 1_000_000))
    os.utime(exe, (2_000_000, 2_000_000))  # built after the last source edit
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "PYPROJECT", pyproject)
    monkeypatch.setattr(release, "INIT", init)
    monkeypatch.setattr(release, "wheel_has_exe", lambda wheel: True)
    ran = []

    def fake_run(cmd, **kw):
        ran.append([str(c) for c in cmd])
        if "build" in cmd:
            (tmp_path / "dist" / "turboadb-1.2.4-py3-none-any.whl").write_bytes(b"")

    monkeypatch.setattr(release, "run", fake_run)
    return release, tmp_path, ran


def test_the_tested_exe_survives_the_version_bump(project):
    """The bump rewrote turboadb/__init__.py, which then looked newer than any
    exe: the one built and tested for the release was always rebuilt."""
    release, root, ran = project
    assert release.main(["patch", "--skip-tests", "--dry-run"]) == 0
    assert not any("build_exe.py" in " ".join(cmd) for cmd in ran)
    bundled = root / "turboadb" / "bin" / "turboadb-gui.exe"
    assert bundled.read_bytes() == b"MZ-tested"
    assert '"1.2.4"' in (root / "turboadb" / "__init__.py").read_text(encoding="utf-8")


def test_a_real_source_edit_still_rebuilds_the_exe(project):
    release, root, ran = project
    os.utime(root / "turboadb" / "__init__.py", (3_000_000, 3_000_000))  # edited after the build
    release.main(["patch", "--skip-tests", "--dry-run"])
    assert any("build_exe.py" in " ".join(cmd) for cmd in ran)


def test_an_unchanged_version_file_is_not_rewritten(project):
    release, root, _ran = project
    init = root / "turboadb" / "__init__.py"
    release.set_version(init, r'__version__\s*=\s*"([^"]+)"', "1.2.3", "turboadb/__init__.py")
    assert os.stat(init).st_mtime == 1_000_000


def test_a_release_without_notes_stops_before_anything_runs(project, capsys):
    """The tests ran against the old version's notes, so the upload went out and
    the Release workflow failed afterwards."""
    release, root, ran = project
    (root / "CHANGELOG.md").write_text("## 1.2.3\n\n- Older.\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        release.main(["patch"])
    assert "no notes for 1.2.4" in str(exc.value) and "## 1.2.4" in str(exc.value)
    assert ran == []  # no tests, no build, no upload
    assert '"1.2.3"' in (root / "turboadb" / "__init__.py").read_text(encoding="utf-8")


def test_a_dry_run_without_notes_warns_and_still_builds(project, capsys):
    release, root, ran = project
    (root / "CHANGELOG.md").write_text("## 1.2.3\n\n- Older.\n", encoding="utf-8")
    assert release.main(["patch", "--skip-tests", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.count("no notes for 1.2.4") == 2  # at the start and again at the end
    assert any("twine" in " ".join(cmd) for cmd in ran)
