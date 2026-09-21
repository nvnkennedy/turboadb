"""The Files page's transfer history wired into a real FileBrowser.

Headless (Qt offscreen); transfers run through a fake thread, no device."""

import os
import threading
import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QObject, pyqtSignal  # noqa: E402

from turboadb.gui import file_browser as fb  # noqa: E402
from turboadb.gui.transfer_log import CANCELLED, DONE, FAILED, QUEUED, RUNNING  # noqa: E402


def _thread_class(started, *, with_result=True):
    class FakeTransfer(QObject):
        progress = pyqtSignal(int)
        done = pyqtSignal(str)
        failed = pyqtSignal(str)
        finished = pyqtSignal()
        if with_result:
            result = pyqtSignal(object)

        def __init__(self, handler, direction, a, b):
            super().__init__()
            self.direction, self.a, self.b = direction, a, b
            self.stopped = False

        def start(self):
            started.append(self)

        def stop(self):
            self.stopped = True

    return FakeTransfer


@pytest.fixture
def browser(qapp, monkeypatch):
    started = []
    monkeypatch.setattr(fb, "_TransferThread", _thread_class(started))
    monkeypatch.setattr(fb, "park_thread", lambda t: None)
    widget = fb.FileBrowser(None)
    widget.started = started
    monkeypatch.setattr(widget, "refresh_local", lambda *a, **k: None)
    monkeypatch.setattr(widget, "refresh_remote", lambda *a, **k: None)
    yield widget
    widget.close_panel()


def _wait(qapp, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _statuses(widget):
    return [item.status for item in widget.transfers]


# --------------------------------------------------------------------------- #
# every queued transfer is recorded
# --------------------------------------------------------------------------- #
def test_queued_transfers_appear_in_the_history(browser):
    browser._enqueue_transfers([("/d/a", "a", "pull"), ("/d/b", "b", "pull")], "go")
    assert _statuses(browser) == [RUNNING, QUEUED]
    assert not browser.transfer_panel.isHidden()
    assert browser.started[0].a == "/d/a"


def test_progress_and_completion_update_the_history(browser):
    browser._enqueue_transfers([("/d/a", "a", "pull"), ("/d/b", "b", "pull")], "go")
    first = browser.started[0]
    first.progress.emit(40)
    assert browser.transfers.items[0].percent == 40
    first.result.emit(type("Result", (), {"size_bytes": 5000, "files": 1, "duration": 2.0})())
    first.done.emit("ok")
    item = browser.transfers.items[0]
    assert (item.status, item.size, item.size_exact, item.duration) == (DONE, 5000, True, 2.0)
    assert _statuses(browser) == [DONE, RUNNING]


def test_a_failure_is_recorded_with_its_error(browser):
    browser._enqueue_transfers([("/d/a", "a", "pull")], "go")
    browser.started[0].failed.emit("adb: error: remote object does not exist")
    item = browser.transfers.items[0]
    assert item.status == FAILED and "does not exist" in item.error


def test_a_late_progress_report_does_not_touch_the_next_transfer(browser):
    browser._enqueue_transfers([("/d/a", "a", "pull"), ("/d/b", "b", "pull")], "go")
    first = browser.started[0]
    first.done.emit("ok")
    first.progress.emit(77)  # arrives after its transfer ended
    assert browser.transfers.items[1].percent == 0


def test_cancel_marks_what_never_ran(browser):
    browser._enqueue_transfers([("/d/a", "a", "pull"), ("/d/b", "b", "pull"),
                                ("/d/c", "c", "pull")], "go")
    browser.cancel_transfers()
    assert _statuses(browser) == [RUNNING, CANCELLED, CANCELLED]
    browser.started[0].failed.emit("adb pull cancelled")
    assert _statuses(browser) == [CANCELLED, CANCELLED, CANCELLED]
    assert browser.bar.isHidden() and browser.btn_cancel_transfer.isHidden()


def test_identical_jobs_queued_twice_keep_their_own_rows(browser):
    job = ("/d/same", "same", "pull")
    browser._enqueue_transfers([job, job], "twice")
    first_id, second_id = [item.id for item in browser.transfers]
    assert browser.started[0]._transfer_id == first_id
    browser.started[0].done.emit("ok")
    assert browser.started[1]._transfer_id == second_id
    assert _statuses(browser) == [DONE, RUNNING]


def test_a_thread_without_a_result_signal_still_works(qapp, monkeypatch):
    """Older test doubles (and anything else) may not have ``result``."""
    started = []
    monkeypatch.setattr(fb, "_TransferThread", _thread_class(started, with_result=False))
    monkeypatch.setattr(fb, "park_thread", lambda t: None)
    widget = fb.FileBrowser(None)
    try:
        widget._enqueue_transfers([("/d/a", "a", "pull")], "go")
        started[0].done.emit("ok")
        assert _statuses(widget) == [DONE]
    finally:
        widget.close_panel()


def test_a_merged_pull_keeps_its_estimate_instead_of_the_overcounted_result(browser):
    browser._enqueue_transfers([("/sdcard/DCIM/.", "C:/dl/DCIM", "pull")], "merge")
    item = browser.transfers.items[0]
    browser.transfers.set_size(item.id, 4096, exact=False)
    browser.started[0].result.emit(
        type("Result", (), {"size_bytes": 999999, "files": 50, "duration": 1.0})())
    assert (item.size, item.size_exact) == (4096, False)


# --------------------------------------------------------------------------- #
# sizes are measured without holding anything up
# --------------------------------------------------------------------------- #
def test_push_sizes_are_measured_from_the_pc(browser, qapp, tmp_path):
    big = tmp_path / "film.mp4"
    big.write_bytes(b"x" * 4096)
    folder = tmp_path / "album"
    folder.mkdir()
    (folder / "1.jpg").write_bytes(b"y" * 100)
    (folder / "2.jpg").write_bytes(b"z" * 28)
    browser._enqueue_transfers([(str(big), "/sdcard/film.mp4", "push"),
                                (str(folder), "/sdcard/album", "push")], "push")
    assert _wait(qapp, lambda: all(item.size is not None for item in browser.transfers))
    sizes = [(item.size, item.files) for item in browser.transfers]
    assert sizes == [(4096, 1), (128, 2)]
    assert browser.transfers.stats().bytes_total == 4224


def test_pull_sizes_are_measured_on_the_device(qapp, monkeypatch):
    started = []
    monkeypatch.setattr(fb, "_TransferThread", _thread_class(started))
    monkeypatch.setattr(fb, "park_thread", lambda t: None)

    class Handler:
        def shell(self, command, timeout=None, safe=None):
            if "@@" not in command:  # the listing the first show would start
                raise RuntimeError("not needed here")
            return type("R", (), {"stdout": "@@0 F 2048\n@@1 D 10\n"})()

    widget = fb.FileBrowser(Handler())
    monkeypatch.setattr(widget, "refresh_local", lambda *a, **k: None)
    monkeypatch.setattr(widget, "refresh_remote", lambda *a, **k: None)
    try:
        widget._enqueue_transfers([("/sdcard/a.mp4", "C:/dl/a.mp4", "pull"),
                                   ("/sdcard/DCIM", "C:/dl/DCIM", "pull")], "pull")
        assert _wait(qapp, lambda: all(item.size is not None for item in widget.transfers))
        assert [(i.size, i.size_exact) for i in widget.transfers] == [(2048, True),
                                                                      (10240, False)]
    finally:
        widget.close_panel()


def test_measuring_pull_sizes_does_not_take_an_adb_slot(qapp, monkeypatch):
    """`du` on a big folder can take a while; like the transfers themselves it
    must not queue this tab's listings behind it."""
    started = []
    monkeypatch.setattr(fb, "_TransferThread", _thread_class(started))
    monkeypatch.setattr(fb, "park_thread", lambda t: None)

    class Gate:
        wrapped = 0

        def wrap(self, fn):
            Gate.wrapped += 1
            return fn

    class Handler:
        def shell(self, command, timeout=None, safe=None):
            if "@@" not in command:
                raise RuntimeError("not needed here")
            return type("R", (), {"stdout": "@@0 F 5\n"})()

    widget = fb.FileBrowser(Handler(), adb_gate=Gate())
    monkeypatch.setattr(widget, "refresh_local", lambda *a, **k: None)
    monkeypatch.setattr(widget, "refresh_remote", lambda *a, **k: None)
    try:
        Gate.wrapped = 0
        widget._enqueue_transfers([("/sdcard/a.txt", "C:/dl/a.txt", "pull")], "pull")
        assert _wait(qapp, lambda: all(item.size is not None for item in widget.transfers))
        assert Gate.wrapped == 0
    finally:
        widget.close_panel()


def test_a_closed_browser_ignores_a_late_measurement(qapp, monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(fb, "_TransferThread", _thread_class(started))
    monkeypatch.setattr(fb, "park_thread", lambda t: None)
    release = threading.Event()
    real = fb.measure_local
    monkeypatch.setattr(fb, "measure_local", lambda paths: (release.wait(5), real(paths))[1])
    widget = fb.FileBrowser(None)
    (tmp_path / "a").write_bytes(b"a")
    widget._enqueue_transfers([(str(tmp_path / "a"), "/sdcard/a", "push")], "go")
    widget.close_panel()
    release.set()
    for _ in range(50):
        qapp.processEvents()
        time.sleep(0.005)
    assert widget.transfers.items[0].size is None  # nothing reached the closed panel


# --------------------------------------------------------------------------- #
# the panel's actions
# --------------------------------------------------------------------------- #
def test_retry_requeues_failed_items_in_fresh_rows(browser):
    browser._enqueue_transfers([("/d/a", "a", "pull"), ("/d/b", "b", "pull")], "go")
    browser.started[0].failed.emit("offline")
    browser.started[1].done.emit("ok")
    failed_id = browser.transfers.items[0].id
    browser._retry_transfers([failed_id])
    assert browser.transfers.get(failed_id) is None  # the old row is gone
    assert [item.status for item in browser.transfers] == [DONE, RUNNING]
    assert browser.started[-1].a == "/d/a"


def test_retry_ignores_items_that_did_not_fail(browser):
    browser._enqueue_transfers([("/d/a", "a", "pull")], "go")
    browser.started[0].done.emit("ok")
    done_id = browser.transfers.items[0].id
    count = len(browser.started)
    browser._retry_transfers([done_id])
    assert len(browser.started) == count and browser.transfers.get(done_id) is not None


def test_open_location_goes_to_the_right_pane(browser, monkeypatch):
    jumps = []
    monkeypatch.setattr(browser, "_jump_remote", lambda path: jumps.append(("device", path)))
    monkeypatch.setattr(browser, "_jump_local", lambda path: jumps.append(("pc", path)))
    browser._enqueue_transfers([("C:/pc/a.txt", "/sdcard/Download/a.txt", "push"),
                                ("/sdcard/b.txt", os.path.join("C:", "dl", "b.txt"), "pull")],
                               "go")
    push, pull = browser.transfers.items
    browser._open_transfer_location(push)
    browser._open_transfer_location(pull)
    assert jumps[0] == ("device", "/sdcard/Download")
    assert jumps[1][0] == "pc" and jumps[1][1].replace("\\", "/").endswith("dl")


def test_the_panel_cancel_action_cancels(browser):
    browser._enqueue_transfers([("/d/a", "a", "pull"), ("/d/b", "b", "pull")], "go")
    browser.transfer_panel.cancel_requested.emit()
    assert browser.started[0].stopped and browser._queue == []


def test_the_summary_toast_is_said_once_per_batch(browser):
    lines = []
    browser.log.connect(lines.append)
    browser._enqueue_transfers([("/d/a", "a", "pull"), ("/d/b", "b", "pull")], "go")
    browser.started[0].done.emit("ok")
    browser.started[1].done.emit("ok")
    assert lines.count("[OK] Pulled 2 items") == 1
    browser._process_queue()  # a stray second drain of the same queue
    assert lines.count("[OK] Pulled 2 items") == 1


def test_details_opening_gives_the_list_room(browser, qapp):
    browser.resize(1000, 800)
    browser.show()
    qapp.processEvents()
    browser._enqueue_transfers([("/d/a", "a", "pull"), ("/d/b", "b", "pull")], "go")
    browser.transfer_panel.render()
    qapp.processEvents()
    assert browser.transfer_panel.details_shown  # a multi-item batch opens them
    assert browser._vsplit.sizes()[1] >= 240
    browser.hide()


# --------------------------------------------------------------------------- #
# another Files tab from this one
# --------------------------------------------------------------------------- #
def test_new_tab_is_requested_with_the_current_folders(browser, tmp_path):
    asked = []
    browser.new_tab_requested.connect(lambda remote, local: asked.append((remote, local)))
    browser.remote_cwd = "/sdcard/DCIM"
    browser.local_cwd = str(tmp_path)
    browser._request_new_tab()
    assert asked == [("/sdcard/DCIM", str(tmp_path))]


def test_new_tab_button_shows_only_when_something_listens(qapp, monkeypatch):
    lonely = fb.FileBrowser(None)
    wired = fb.FileBrowser(None)
    wired.new_tab_requested.connect(lambda *_a: None)
    try:
        for widget in (lonely, wired):
            monkeypatch.setattr(widget, "refresh_remote", lambda *a, **k: None)
            widget.show()
        qapp.processEvents()
        assert lonely.btn_new_tab.isHidden()
        assert not wired.btn_new_tab.isHidden()
    finally:
        for widget in (lonely, wired):
            widget.hide()
            widget.close_panel()


def test_the_new_tab_shortcut_is_not_the_main_windows_ctrl_t():
    from PyQt5.QtWidgets import QShortcut

    widget = fb.FileBrowser(None)
    try:
        keys = {s.key().toString() for s in widget.findChildren(QShortcut)}
        assert "Ctrl+Shift+T" in keys
        assert "Ctrl+T" not in keys and "Ctrl+W" not in keys
    finally:
        widget.close_panel()


def test_local_start_opens_the_pc_pane_there(qapp, tmp_path):
    widget = fb.FileBrowser(None, local_start=str(tmp_path))
    try:
        assert widget.local_cwd == str(tmp_path)
    finally:
        widget.close_panel()
    missing = fb.FileBrowser(None, local_start=str(tmp_path / "gone"))
    try:
        assert missing.local_cwd == os.path.expanduser("~")  # never a broken start
    finally:
        missing.close_panel()


# --------------------------------------------------------------------------- #
# the real worker hands over its result before it says done
# --------------------------------------------------------------------------- #
def test_the_transfer_thread_emits_its_result_then_done(qapp):
    from turboadb.results import TransferResult

    class Handler:
        def push(self, a, b, **kwargs):
            kwargs["on_progress"](100)
            return TransferResult(a, b, "push", 1234, 0.5)

    order = []
    thread = fb._TransferThread(Handler(), "push", "C:/a.bin", "/sdcard/a.bin")
    thread.result.connect(lambda res: order.append(("result", res.size_bytes)))
    thread.done.connect(lambda text: order.append(("done", text[:6])))
    thread.run()  # synchronously: the signals are direct here
    assert order == [("result", 1234), ("done", "Pushed")]


def test_the_write_access_retry_replaces_the_refused_rows(browser, monkeypatch):
    """Refused on permissions -> made writable -> retried. The failed rows used
    to stay, so "Retry N failed" still offered files that had since arrived."""
    offers = []
    monkeypatch.setattr(browser, "_offer_write_access",
                        lambda folder, error, action, retry=None: offers.append(retry) or True)
    browser._enqueue_transfers([("C:/pc/a.txt", "/system/a.txt", "push"),
                                ("C:/pc/b.txt", "/system/b.txt", "push")], "go")
    browser.started[0].failed.emit("adb: error: failed to copy: Read-only file system")
    browser.started[1].done.emit("ok")
    assert len(offers) == 1 and len(browser.transfers.retryable()) == 1
    offers[0]()  # the device is writable now: the retry runs
    assert browser.transfers.retryable() == []  # the refused row is gone
    assert [item.status for item in browser.transfers] == [DONE, RUNNING]
    browser.started[-1].done.emit("ok")
    assert browser.transfers.retryable() == []
