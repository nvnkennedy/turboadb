"""A folder transfer whose target appeared after it was planned (a retry after
a partial copy, or the same folder queued twice) merges instead of nesting."""
import os

import pytest

pytest.importorskip("PyQt5")

from turboadb.gui import file_browser as fb  # noqa: E402


class _Handler:
    def __init__(self):
        self.calls = []

    def push(self, a, b, **kw):
        self.calls.append(("push", a, b))
        return "pushed"

    def pull(self, a, b, **kw):
        self.calls.append(("pull", a, b))
        return "pulled"


def _run(direction, a, b, probe, monkeypatch):
    monkeypatch.setattr(fb, "_probe_remote", probe)
    handler = _Handler()
    t = fb._TransferThread(handler, direction, a, b)
    t.run()   # synchronously: the logic under test runs on the worker
    return t, handler.calls


def _kinds(mapping):
    def probe(handler, paths):
        return {p: (mapping.get(p, "n"), False, p) for p in paths}
    return probe


def test_a_retried_folder_push_merges_into_the_target_it_created(tmp_path, monkeypatch):
    src = tmp_path / "logs"
    src.mkdir()
    t, calls = _run("push", str(src), "/sdcard/logs", _kinds({"/sdcard/logs": "d"}), monkeypatch)
    assert calls == [("push", os.path.join(str(src), "."), "/sdcard/logs")]
    assert t.a == os.path.join(str(src), ".")


def test_a_folder_push_to_a_free_target_is_unchanged(tmp_path, monkeypatch):
    src = tmp_path / "logs"
    src.mkdir()
    _t, calls = _run("push", str(src), "/sdcard/logs", _kinds({}), monkeypatch)
    assert calls == [("push", str(src), "/sdcard/logs")]


def test_a_file_push_is_never_probed(tmp_path, monkeypatch):
    src = tmp_path / "a.txt"
    src.write_text("x")

    def probe(handler, paths):
        raise AssertionError("a file needs no probe")

    _t, calls = _run("push", str(src), "/sdcard/a.txt", probe, monkeypatch)
    assert calls == [("push", str(src), "/sdcard/a.txt")]


def test_a_retried_folder_pull_merges_into_the_local_folder(tmp_path, monkeypatch):
    dst = tmp_path / "logs"
    dst.mkdir()
    t, calls = _run("pull", "/sdcard/logs", str(dst), _kinds({"/sdcard/logs": "d"}), monkeypatch)
    assert calls == [("pull", "/sdcard/logs/.", str(dst))]
    assert t.a == "/sdcard/logs/."   # what _on_transfer_result's merged_pull check reads


def test_a_file_pulled_onto_a_local_folder_is_left_alone(tmp_path, monkeypatch):
    dst = tmp_path / "a.txt"
    dst.mkdir()
    _t, calls = _run("pull", "/sdcard/a.txt", str(dst), _kinds({"/sdcard/a.txt": "f"}), monkeypatch)
    assert calls == [("pull", "/sdcard/a.txt", str(dst))]


def test_a_failed_probe_runs_the_job_as_planned(tmp_path, monkeypatch):
    src = tmp_path / "logs"
    src.mkdir()

    def probe(handler, paths):
        raise RuntimeError("could not check the device paths: offline")

    _t, calls = _run("push", str(src), "/sdcard/logs", probe, monkeypatch)
    assert calls == [("push", str(src), "/sdcard/logs")]


def test_the_merge_form_is_kept(tmp_path, monkeypatch):
    src = tmp_path / "logs"
    src.mkdir()
    merged = os.path.join(str(src), ".")
    _t, calls = _run("push", merged, "/sdcard/logs", _kinds({"/sdcard/logs": "d"}), monkeypatch)
    assert calls == [("push", merged, "/sdcard/logs")]
