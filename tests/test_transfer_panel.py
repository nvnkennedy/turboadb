"""The Files page's transfer panel: rendering, filtering and actions.

Headless (Qt offscreen); no device, adb or network is used."""

import pytest

pytest.importorskip("PyQt5")

from turboadb.gui.transfer_log import (  # noqa: E402
    CANCELLED, DONE, FAILED, RUNNING, TransferLog, TransferStats,
)


class Clock:
    def __init__(self, t=500.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, seconds):
        self.t += seconds


def _jobs(n, direction="push", prefix="/pc/f"):
    return [(f"{prefix}{i}.bin", f"/sdcard/f{i}.bin", direction) for i in range(n)]


@pytest.fixture
def panel(qapp):
    from turboadb.gui.transfer_panel import TransferPanel

    clock = Clock()
    log = TransferLog(clock=clock)
    widget = TransferPanel(log)
    widget.resize(900, 400)
    widget.clock = clock
    widget.log_model = log
    yield widget
    widget.close_panel()
    widget.deleteLater()
    qapp.processEvents()


# --------------------------------------------------------------------------- #
# the summary line
# --------------------------------------------------------------------------- #
def test_summary_describes_an_active_batch_by_bytes():
    from turboadb.gui.transfer_panel import summary_text

    stats = TransferStats(total=57, done=23, running=1, queued=33, bytes_total=3 * 1024 ** 3,
                          bytes_done=1024 ** 3, by_size=True, speed=18 * 1024 ** 2, eta=92,
                          directions=("push",))
    assert summary_text(stats) == (
        "Pushing 24 of 57 · 1.0 GB of 3.0 GB · 18.0 MB/s · about 1:32 left"
    )


def test_summary_while_sizes_are_being_measured():
    from turboadb.gui.transfer_panel import summary_text

    stats = TransferStats(total=4, running=1, queued=3, measuring=2, directions=("pull",))
    assert summary_text(stats) == "Pulling 1 of 4 · measuring 2 items…"


def test_summary_of_a_finished_batch_with_failures():
    from turboadb.gui.transfer_panel import summary_text

    stats = TransferStats(total=5, done=3, failed=1, cancelled=1, bytes_total=1000,
                          bytes_estimated=True, bytes_done=600, speed=0, elapsed=161,
                          directions=("push", "pull"))
    assert summary_text(stats) == (
        "Last batch: 3 of 5 items transferred · 600 B of ~1000 B · in 2:41 · "
        "1 failed · 1 cancelled"
    )
    assert summary_text(TransferStats()) == ""


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #
def test_rows_are_appended_without_losing_the_selection(panel, qapp):
    log = panel.log_model
    log.add(_jobs(3))
    panel.render()
    panel.set_details_shown(True, by_user=True)
    panel.show()
    qapp.processEvents()
    panel.table.selectRow(1)
    log.add(_jobs(2, prefix="/pc/more"))
    panel.render()
    assert panel.model.rowCount() == 5
    assert [item.name for item in panel.selected_items()] == ["f1.bin"]


def test_rows_show_status_size_progress_speed_and_errors(panel):
    from turboadb.gui.transfer_panel import (
        COL_DETAILS, COL_DIRECTION, COL_PROGRESS, COL_SIZE, COL_SPEED, COL_STATUS, COL_TIME,
    )

    log, clock = panel.log_model, panel.clock
    done_id, fail_id, queued_id = log.add(_jobs(3))
    log.set_size(done_id, 2 * 1024 ** 2)
    log.start(done_id)
    clock.tick(2)
    log.finish(done_id, DONE)
    log.start(fail_id)
    log.progress(fail_id, 30)
    log.finish(fail_id, FAILED, error="Permission denied")
    panel.render()
    model = panel.model

    def cell(row, column):
        return model.data(model.index(row, column))

    assert cell(0, COL_STATUS) == "Done"
    assert cell(0, COL_DIRECTION) == "PC → Device"
    assert cell(0, COL_SIZE) == "2.0 MB"
    assert cell(0, COL_PROGRESS) == "100%"
    assert cell(0, COL_SPEED) == "1.0 MB/s"
    assert cell(0, COL_TIME) == "0:02"
    assert cell(0, COL_DETAILS) == "/sdcard/f0.bin"
    assert cell(1, COL_STATUS) == "Failed"
    assert cell(1, COL_PROGRESS) == "30%"
    assert cell(1, COL_DETAILS) == "Permission denied"
    assert cell(2, COL_STATUS) == "Queued"
    assert cell(2, COL_SIZE) == "measuring…"
    assert cell(2, COL_PROGRESS) == ""
    assert "Permission denied" in model.data(model.index(1, 0), 3)  # ToolTipRole


def test_a_folder_estimate_is_marked_approximate(panel):
    from turboadb.gui.transfer_panel import COL_SIZE

    log = panel.log_model
    [item_id] = log.add(_jobs(1, direction="pull"))
    log.set_size(item_id, 8192, exact=False, files=12)
    panel.render()
    assert panel.model.data(panel.model.index(0, COL_SIZE)) == "~8.0 KB · 12 files"


def test_only_changed_rows_are_repainted(panel):
    log = panel.log_model
    ids = log.add(_jobs(5))
    panel.render()
    spans = []
    panel.model.dataChanged.connect(lambda a, b: spans.append((a.row(), b.row())))
    log.start(ids[3])
    panel.render()
    assert spans == [(3, 3)]
    spans.clear()
    panel.render()  # nothing new, but a running row's time still moves
    assert spans == [(3, 3)]
    log.finish(ids[3], DONE)
    panel.render()
    spans.clear()
    panel.render()
    assert spans == []


def test_clearing_resets_the_rows(panel):
    log = panel.log_model
    a, b = log.add(_jobs(2))
    log.start(a)
    log.finish(a, DONE)
    log.start(b)
    log.finish(b, DONE)
    panel.render()
    assert panel.model.rowCount() == 2
    panel._clear_finished()
    assert panel.model.rowCount() == 0
    assert panel.isHidden()  # nothing left to show


def test_the_filter_shows_only_failed_and_cancelled(panel):
    from turboadb.gui.transfer_panel import FILTERS

    log = panel.log_model
    a, b, c = log.add(_jobs(3))
    log.start(a)
    log.finish(a, DONE)
    log.start(b)
    log.finish(b, FAILED, error="x")
    log.cancel_queued()
    panel.render()
    panel.filter.setCurrentIndex([label for label, _ in FILTERS].index("Failed"))
    proxy = panel.table.model()
    names = [proxy.data(proxy.index(row, 1)) for row in range(proxy.rowCount())]
    assert names == ["f1.bin", "f2.bin"]
    panel.filter.setCurrentIndex(0)
    assert proxy.rowCount() == 3


# --------------------------------------------------------------------------- #
# the panel
# --------------------------------------------------------------------------- #
def test_the_panel_stays_hidden_until_something_is_queued(panel):
    assert panel.isHidden()
    panel.log_model.add(_jobs(1))
    panel.refresh()
    assert not panel.isHidden()


def test_a_multi_item_batch_opens_the_details_once(panel):
    log = panel.log_model
    log.add(_jobs(1))
    panel.render()
    assert not panel.details_shown  # a single transfer needs no list
    for item in log.items:
        log.start(item.id)
        log.finish(item.id, DONE)
    log.add(_jobs(3, prefix="/pc/b"))
    panel.render()
    assert panel.details_shown


def test_the_users_choice_about_details_is_kept(panel):
    log = panel.log_model
    panel.btn_details.click()  # open ...
    panel.btn_details.click()  # ... and close it again: that's a choice
    assert not panel.details_shown
    log.add(_jobs(4))
    panel.render()
    assert not panel.details_shown


def test_retry_offers_the_failed_items(panel):
    log = panel.log_model
    a, b = log.add(_jobs(2))
    log.start(a)
    log.finish(a, FAILED, error="x")
    log.start(b)
    log.finish(b, DONE)
    panel.render()
    assert not panel.btn_retry.isHidden()
    assert panel.btn_retry.text() == "Retry 1 failed"
    asked = []
    panel.retry_requested.connect(asked.append)
    panel.btn_retry.click()
    assert asked == [[a]]


def test_retry_is_hidden_while_a_batch_is_running(panel):
    log = panel.log_model
    a, b = log.add(_jobs(2))
    log.start(a)
    log.finish(a, FAILED, error="x")
    log.start(b)
    panel.render()
    assert panel.btn_retry.isHidden()


def test_double_click_asks_to_open_the_location(panel, qapp):
    log = panel.log_model
    log.add(_jobs(2))
    panel.set_details_shown(True, by_user=True)
    panel.render()
    opened = []
    panel.open_requested.connect(opened.append)
    panel._on_double_click(panel.table.model().index(1, 1))
    assert [item.name for item in opened] == ["f1.bin"]


def test_removing_selected_rows_keeps_active_ones(panel, qapp):
    log = panel.log_model
    a, b = log.add(_jobs(2))
    log.start(a)
    log.finish(a, DONE)
    log.start(b)  # still running
    panel.set_details_shown(True, by_user=True)
    panel.show()
    panel.render()
    qapp.processEvents()
    panel.table.selectAll()
    panel._remove_selected()
    assert [item.id for item in log] == [b]


def test_copy_report_goes_to_the_clipboard(panel, qapp):
    from PyQt5.QtWidgets import QApplication

    log = panel.log_model
    [item_id] = log.add(_jobs(1))
    log.start(item_id)
    log.finish(item_id, FAILED, error="No space left on device")
    panel._copy_report()
    text = QApplication.clipboard().text()
    assert "TurboADB transfer report" in text and "No space left on device" in text


def test_the_menu_offers_cancel_only_while_active(panel):
    log = panel.log_model
    [item_id] = log.add(_jobs(1))
    log.start(item_id)
    panel._fill_menu()
    labels = [action.text() for action in panel._menu.actions()]
    assert "Cancel all transfers" in labels
    cancelled = []
    panel.cancel_requested.connect(lambda: cancelled.append(True))
    next(a for a in panel._menu.actions() if a.text() == "Cancel all transfers").trigger()
    assert cancelled == [True]
    log.finish(item_id, DONE)
    panel._fill_menu()
    assert "Cancel all transfers" not in [action.text() for action in panel._menu.actions()]


def test_the_table_paints_progress_without_errors(panel, qapp):
    log, clock = panel.log_model, panel.clock
    ids = log.add(_jobs(4))
    log.set_size(ids[0], 100)
    log.start(ids[0])
    log.finish(ids[0], DONE)
    log.start(ids[1])
    log.progress(ids[1], 40)
    log.start(ids[2])
    log.finish(ids[2], FAILED, error="x")
    clock.tick(1)
    panel.set_details_shown(True, by_user=True)
    panel.show()
    panel.render()
    qapp.processEvents()
    assert not panel.grab().isNull()  # every delegate path painted


def test_render_is_coalesced_and_stops_after_close(panel, qapp):
    log = panel.log_model
    log.add(_jobs(2))
    panel.refresh()
    assert panel._render_timer.isActive() and panel._tick.isActive()
    panel.close_panel()
    assert not panel._render_timer.isActive() and not panel._tick.isActive()
    panel.refresh()  # after close nothing restarts
    assert not panel._render_timer.isActive()


def test_the_tick_stops_once_the_batch_is_idle(panel):
    log = panel.log_model
    [item_id] = log.add(_jobs(1))
    log.start(item_id)
    panel.refresh()
    assert panel._tick.isActive()
    log.finish(item_id, CANCELLED)
    panel.render()
    assert not panel._tick.isActive()  # no idle wake-ups


def test_a_running_item_reports_live_status(panel):
    from turboadb.gui.transfer_panel import COL_STATUS

    log = panel.log_model
    [item_id] = log.add(_jobs(1))
    log.start(item_id)
    panel.render()
    assert panel.model.data(panel.model.index(0, COL_STATUS)) == "Copying"
    assert log.get(item_id).status == RUNNING


def test_a_sub_second_transfer_does_not_read_zero(panel):
    from turboadb.gui.transfer_panel import COL_TIME

    log, clock = panel.log_model, panel.clock
    fast, slow = log.add(_jobs(2))
    log.start(fast)
    clock.tick(0.3)
    log.finish(fast, DONE)
    log.start(slow)
    clock.tick(75)
    log.finish(slow, DONE)
    panel.render()
    assert panel.model.data(panel.model.index(0, COL_TIME)) == "< 1 s"
    assert panel.model.data(panel.model.index(1, COL_TIME)) == "1:15"


def test_a_quick_batch_that_failed_still_opens_the_details(panel):
    """A batch of small files can finish before the first render; one that
    failed must still show what failed."""
    log = panel.log_model
    a, b = log.add(_jobs(2))
    log.start(a)
    log.finish(a, DONE)
    log.start(b)
    log.finish(b, FAILED, error="Permission denied")
    panel.render()  # the first render sees an idle batch
    assert panel.details_shown


def test_a_quick_clean_batch_leaves_the_details_closed(panel):
    log = panel.log_model
    for item_id in log.add(_jobs(2)):
        log.start(item_id)
        log.finish(item_id, DONE)
    panel.render()
    assert not panel.details_shown


def test_the_collapsed_panel_hands_spare_room_back_to_the_panes(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QLabel, QSplitter

    from turboadb.gui.transfer_panel import TransferPanel

    log = TransferLog(clock=Clock())
    splitter = QSplitter(Qt.Vertical)
    splitter.addWidget(QLabel("panes"))
    panel = TransferPanel(log)
    splitter.addWidget(panel)
    splitter.resize(800, 700)
    splitter.show()
    try:
        [item_id] = log.add(_jobs(1))
        log.start(item_id)
        log.finish(item_id, DONE)
        panel.render()
        qapp.processEvents()
        splitter.setSizes([500, 200])  # a slot much taller than the header
        panel.render()
        qapp.processEvents()
        panes, slot = splitter.sizes()
        assert slot == panel.maximumHeight()  # no empty band under the summary
        assert panes > 500  # the spare room went back to the file panes
    finally:
        panel.close_panel()
        splitter.hide()
        splitter.deleteLater()
        qapp.processEvents()
