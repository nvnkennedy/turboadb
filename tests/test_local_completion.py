"""Tab completion in the PowerShell / CMD terminals.

It reads folders (the current one, every PATH entry), so it runs on a worker
thread: on a share whose server stopped answering each read stalled the whole
window.  A quoted path with spaces completes further (``cd "My Folder"\\su``).
"""
import os
import threading
import time

import pytest

pytest.importorskip("PyQt5")

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")


@pytest.fixture
def widget(qapp, tmp_path):
    from turboadb.gui.device_tab import _LocalShellWidget

    widget = _LocalShellWidget("cmd")
    widget._shell_cwd = str(tmp_path)
    yield widget
    widget.close_panel()
    widget.deleteLater()


def _wait_for_completion(qapp, widget, limit=10.0):
    deadline = time.monotonic() + limit
    while widget._completion_thread is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()


@windows_only
def test_quoted_paths_with_spaces_complete_further(widget, tmp_path):
    (tmp_path / "My Folder" / "subdir").mkdir(parents=True)
    expected = ('cd "My Folder\\subdir"', [])
    assert widget._local_complete('cd "My Folder"\\su') == expected
    assert widget._local_complete('cd "My Folder\\su') == expected  # quote still open
    # a folder completion closed with its quote: Tab goes into that folder
    assert widget._local_complete('cd "My Folder"') == expected
    (tmp_path / "My Folder" / "subway").mkdir()
    line, options = widget._local_complete('cd "My Folder"\\su')
    assert line == 'cd "My Folder\\sub"'
    assert sorted(options) == ['"My Folder\\subdir"', '"My Folder\\subway"']


@windows_only
def test_tab_completes_on_a_worker_thread_with_the_folder_of_the_moment(qapp, widget, tmp_path,
                                                                        monkeypatch):
    (tmp_path / "target-alpha").mkdir()
    threads = []
    real = type(widget)._local_complete

    def spy(self, line, **kwargs):
        threads.append(threading.get_ident())
        return real(self, line, **kwargs)

    monkeypatch.setattr(type(widget), "_local_complete", spy)
    widget.term._set_line("cd tar")
    widget.term._do_complete()  # returns at once
    widget._shell_cwd = str(tmp_path / "elsewhere")  # a prompt moved on meanwhile
    _wait_for_completion(qapp, widget)
    assert threads and threads[0] != threading.get_ident()
    assert widget.term._line == "cd target-alpha\\"


def test_a_stale_result_never_replaces_newer_typing(qapp, widget):
    widget.term._set_line("adb dev")
    widget.term._do_complete()
    widget.term._set_line("adb devices -l")  # typed on before the result came
    _wait_for_completion(qapp, widget)
    assert widget.term._line == "adb devices -l"


def test_the_path_command_list_is_built_off_the_ui_thread(qapp, widget, tmp_path, monkeypatch):
    from turboadb.gui.device_tab import _LocalShellWidget

    seen = []
    done = threading.Event()

    def record(cls, env=None):
        seen.append((threading.get_ident(), env))
        done.set()
        return set()

    monkeypatch.setattr(_LocalShellWidget, "_get_path_commands", classmethod(record))

    class Session:
        env = {"PATH": str(tmp_path)}

    widget._start_probes(Session())
    assert done.wait(5)
    assert seen[0][0] != threading.get_ident() and seen[0][1] == Session.env
    seen.clear()
    widget._start_probes(object())  # a stand-in session without an environment
    time.sleep(0.1)
    assert seen == []
