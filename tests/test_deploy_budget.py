"""The overall time budget of a remote deploy reports hosts as they stand.

When it ran out, every host not yet reported was called skipped, including
one still being deployed (which could still end up sharing its devices) and
one that had already finished; and `turboadb deploy-serve` then sat, after
printing its summary, until the WinRM calls still running timed out.
"""
import threading
import time

import pytest


@pytest.fixture
def deploy(monkeypatch):
    import turboadb.remote_deploy as rd

    monkeypatch.setattr(rd, "_ensure_winrm", lambda say=None: True)
    gate = threading.Event()
    yield rd, gate
    gate.set()  # let the stragglers go


def test_a_host_that_finished_meanwhile_is_reported_as_it_ended(deploy, monkeypatch):
    rd, gate = deploy

    def worker(host, *a, **kw):
        if host == "slow":
            gate.wait(10)
        return True, [f"[OK] {host}"]

    monkeypatch.setattr(rd, "_deploy_one", worker)
    said = []
    rc = rd.deploy_serve(["slow", "quick"], "D\\u", "pw", on_status=said.append,
                         total_timeout=0.3, max_workers=2)
    assert rc == 1
    assert "[OK] quick" in said  # not "skipped": it had finished
    assert any(line.startswith("[ERROR] slow: still running") for line in said)
    assert not any("Skipped" in line for line in said)


def test_the_run_ends_with_its_budget_not_with_the_stragglers(deploy, monkeypatch):
    rd, gate = deploy
    daemon = []

    def worker(host, *a, **kw):
        daemon.append(threading.current_thread().daemon)
        gate.wait(10)
        return True, [f"[OK] {host}"]

    monkeypatch.setattr(rd, "_deploy_one", worker)
    said = []
    started = time.monotonic()
    rc = rd.deploy_serve(["a", "b", "c"], "D\\u", "pw", on_status=said.append,
                         total_timeout=0.2, max_workers=2)
    assert rc == 1 and time.monotonic() - started < 2.0
    # the interpreter does not wait for them on the way out either
    assert daemon and all(daemon)
    assert [line for line in said if "Skipped" in line][0].endswith(": c")


def test_without_a_budget_every_host_is_waited_for(deploy, monkeypatch):
    rd, _gate = deploy

    def worker(host, *a, **kw):
        time.sleep(0.2)
        return host != "b", [f"[{'OK' if host != 'b' else 'ERROR'}] {host}"]

    monkeypatch.setattr(rd, "_deploy_one", worker)
    said = []
    assert rd.deploy_serve(["a", "b", "c"], "D\\u", "pw", on_status=said.append,
                           total_timeout=None, max_workers=1) == 1
    assert said[-3:] == ["[OK] a", "[ERROR] b", "[OK] c"]


def test_a_host_whose_worker_failed_is_an_error_line(deploy, monkeypatch):
    rd, _gate = deploy

    def worker(host, *a, **kw):
        raise ValueError("bad host name")

    monkeypatch.setattr(rd, "_deploy_one", worker)
    said = []
    assert rd.deploy_serve(["x"], "D\\u", "pw", on_status=said.append) == 1
    assert said == ["[ERROR] x: bad host name"]
