"""Self-update knows which TurboADB it is updating.

pip upgrades the copy this Python has installed. A source checkout (``python
main.py``, even one installed in editable mode) is not that copy, so pip must
not be pointed at it; and pip exiting 0 is no proof that anything newer was
installed — a package index that lags behind PyPI answers "already satisfied".
"""
import json
import os
import sys
import types

import pytest

import turboadb.cli as cli
import turboadb.update as update

HERE = os.path.join(update._PACKAGE_DIR, "__init__.py")


@pytest.fixture(autouse=True)
def _fresh_copy(monkeypatch):
    """running_copy() asks once per process; every test asks again."""
    monkeypatch.setattr(update, "_RUNNING_COPY", {})


def _probe(monkeypatch, found):
    monkeypatch.setattr(update, "_probe_installed", lambda *a, **k: found)


# --------------------------------------------------------------------------- #
# which copy runs
# --------------------------------------------------------------------------- #
def test_a_normal_install_is_the_running_copy(monkeypatch):
    _probe(monkeypatch, {"file": HERE, "version": "2.5.0", "dist_file": HERE, "editable": False})
    copy = update.running_copy()
    assert copy["same"] is True and copy["pip_managed"] is True and copy["note"] is None
    assert update.can_self_update() and update.self_update_blocker() is None


def test_a_checkout_next_to_an_older_install_is_reported(monkeypatch, tmp_path):
    other = str(tmp_path / "site-packages" / "turboadb" / "__init__.py")
    _probe(monkeypatch, {"file": other, "version": "2.0.0", "dist_file": other, "editable": False})
    copy = update.running_copy()
    assert copy["same"] is False and copy["installed"] == "2.0.0"
    assert copy["installed_path"] == os.path.dirname(other)
    assert "2.0.0" in copy["note"] and update._PACKAGE_DIR in copy["note"]
    blocker = update.self_update_blocker()
    assert blocker and "git pull" in blocker and not update.can_self_update()


def test_an_editable_checkout_is_not_replaced_by_a_release(monkeypatch):
    _probe(monkeypatch, {"file": HERE, "version": "2.5.0", "dist_file": "gone", "editable": True})
    copy = update.running_copy()
    assert copy["same"] is True and copy["editable"] is True and copy["pip_managed"] is False
    assert "editable" in update.self_update_blocker()


def test_nothing_installed_and_an_unanswered_probe(monkeypatch):
    _probe(monkeypatch, {"file": None})
    copy = update.running_copy()
    assert copy["same"] is False and "no TurboADB installed" in copy["note"]
    assert not update.can_self_update()

    monkeypatch.setattr(update, "_RUNNING_COPY", {})
    _probe(monkeypatch, None)  # couldn't ask: nothing is blocked on a guess
    copy = update.running_copy()
    assert copy["same"] is None and copy["note"] is None and update.can_self_update()


def test_the_answer_is_kept_for_the_process(monkeypatch):
    asked = []
    monkeypatch.setattr(update, "_probe_installed",
                        lambda *a, **k: asked.append(1) or {"file": HERE, "dist_file": HERE})
    update.running_copy()
    update.running_copy()
    update.can_self_update()
    assert asked == [1]


def test_the_frozen_exe_never_asks(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(update, "_probe_installed", lambda *a, **k: pytest.fail("probed"))
    assert "standalone executable" in update.self_update_blocker()
    assert update.running_copy()["same"] is True


def test_the_probe_runs_in_a_fresh_interpreter_outside_the_checkout():
    """The real probe: it reports what this Python imports from a neutral
    folder, whatever is installed here."""
    found = update._probe_installed()
    assert isinstance(found, dict) and "file" in found
    assert update._neutral_cwd() == os.path.dirname(sys.executable)


def test_children_start_in_the_interpreters_folder(monkeypatch):
    """`python -c`/`-m` import from the current folder first: started in a
    checkout they ran the checkout instead of what pip had just upgraded."""
    seen = []

    def fake_run(cmd, **kw):
        seen.append(kw.get("cwd"))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    update._fresh_report()
    update._probe_installed()
    monkeypatch.setattr(update.subprocess, "Popen", lambda cmd, **kw: seen.append(kw.get("cwd")))
    update._spawn()
    assert seen == [os.path.dirname(sys.executable)] * 3


# --------------------------------------------------------------------------- #
# run_upgrade
# --------------------------------------------------------------------------- #
@pytest.fixture
def pip(monkeypatch):
    """pip succeeds; the version installed afterwards is ``state['installed']``."""
    state = {"cmds": [], "installed": "2.5.0", "tools_refreshed": False}
    monkeypatch.setattr(update, "can_self_update", lambda: True)
    monkeypatch.setattr(update, "current_version", lambda: "2.5.0")

    def fake_run(cmd, **kw):
        state["cmds"].append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def fresh_report(upgrade_tools=True, timeout=900.0):
        state["tools_refreshed"] |= bool(upgrade_tools)
        return {"version": state["installed"], "adb": "36.0.0", "scrcpy": "3.1", "errors": {}}

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "_fresh_report", fresh_report)
    return state


def test_an_upgrade_that_installs_nothing_newer_fails_before_the_tools(pip):
    """The GUI said 'Updated to TurboADB 2.5.0', restarted, and offered the same
    update again, forever, while pip's mirror lagged behind PyPI."""
    res = update.run_upgrade()
    assert res["ok"] is False and res["unchanged"] is True and res["new"] == "2.5.0"
    assert "does not have the new release yet" in res["error"]
    assert pip["tools_refreshed"] is False  # nothing else was touched
    assert pip["cmds"][0][-1] == "turboadb"


def test_the_version_pypi_offered_is_pinned(pip):
    pip["installed"] = "2.6.0"
    res = update.run_upgrade(expected="2.6.0")
    assert res["ok"] is True and res["new"] == "2.6.0" and res["unchanged"] is False
    assert pip["cmds"][0][-1] == "turboadb==2.6.0" and pip["tools_refreshed"] is True

    pip["installed"] = "2.5.5"  # newer than before, but not what was offered
    res = update.run_upgrade(expected="2.6.0")
    assert res["ok"] is False and "(not 2.6.0)" in res["error"]


def test_a_refresh_only_run_may_leave_the_version_alone(pip):
    """`turboadb self-update` with nothing newer on PyPI refreshes adb/scrcpy."""
    res = update.run_upgrade(allow_unchanged=True)
    assert res["ok"] is True and res["unchanged"] is True and pip["tools_refreshed"] is True


def test_only_a_plain_version_is_pinned():
    assert update._pip_upgrade_cmd("2.6.0")[-1] == "turboadb==2.6.0"
    assert update._pip_upgrade_cmd(None)[-1] == "turboadb"
    assert update._pip_upgrade_cmd("2.6.0; rm -rf /")[-1] == "turboadb"


def test_run_upgrade_says_why_it_cannot_update_a_checkout(monkeypatch, tmp_path):
    other = str(tmp_path / "turboadb" / "__init__.py")
    _probe(monkeypatch, {"file": other, "version": "2.0.0", "dist_file": other})
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: pytest.fail("pip ran"))
    res = update.run_upgrade()
    assert res["ok"] is False and "git pull" in res["error"]


# --------------------------------------------------------------------------- #
# turboadb self-update
# --------------------------------------------------------------------------- #
@pytest.fixture
def cli_upgrade(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(update, "self_update_blocker", lambda: None)
    monkeypatch.setattr(update, "current_version", lambda: "2.5.0")

    def go(argv, latest, result):
        monkeypatch.setattr(update, "pypi_latest", lambda *a, **k: latest)
        monkeypatch.setattr(update, "run_upgrade",
                            lambda notify=None, **kw: calls.append(kw) or dict(result))
        rc = cli.main(argv)
        out, err = capsys.readouterr()
        return rc, out, err

    go.calls = calls
    return go


def test_cli_refresh_only_says_up_to_date(cli_upgrade):
    result = {"ok": True, "old": "2.5.0", "new": "2.5.0", "unchanged": True,
              "adb": "36.0.0", "scrcpy": "3.1", "error": None}
    rc, out, _ = cli_upgrade(["self-update"], "2.5.0", result)
    assert rc == 0 and "TurboADB 2.5.0 is already up to date" in out and "Updated" not in out
    assert cli_upgrade.calls == [{"expected": None, "allow_unchanged": True}]


def test_cli_upgrade_pins_the_offered_version_and_prints_one_json_document(cli_upgrade):
    result = {"ok": True, "old": "2.5.0", "new": "2.6.0", "unchanged": False,
              "adb": None, "scrcpy": None, "error": None}
    rc, out, err = cli_upgrade(["self-update", "--json"], "2.6.0", result)
    assert rc == 0 and json.loads(out) == result  # one document, nothing else on stdout
    assert cli_upgrade.calls == [{"expected": "2.6.0", "allow_unchanged": False}]
    failed = dict(result, ok=False, new="2.5.0", error="pip finished, but …")
    rc, out, _ = cli_upgrade(["--json", "self-update"], "2.6.0", failed)
    assert rc == 1 and json.loads(out)["ok"] is False


def test_cli_refuses_a_checkout_with_the_reason(monkeypatch, capsys):
    monkeypatch.setattr(update, "self_update_blocker", lambda: "TurboADB runs from a checkout")
    monkeypatch.setattr(update, "pypi_latest", lambda *a, **k: pytest.fail("asked PyPI"))
    assert cli.main(["self-update"]) == 1
    err = capsys.readouterr().err
    assert "TurboADB runs from a checkout" in err and "turboadb upgrade-tools" in err
    assert cli.main(["self-update", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
