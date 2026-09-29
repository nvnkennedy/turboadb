"""Device commands from the screen's controls: one rule for a failed result,
and no dispatcher thread left behind.

- The keyboard, the screen's taps and keys and a display's buttons each
  decided for themselves whether a command failed, and the copies had drifted:
  a failed result without error text read "None" under one, "failed" under
  another. They now share one rule.
- A screen view closed before any input was sent parked its dispatcher, which
  had never started: it never finished, so it stayed parked for the rest of
  the process.

Headless; no device or adb is used.
"""

import pytest

pytest.importorskip("PyQt5")


def test_one_rule_for_a_device_commands_result():
    from turboadb.gui.device_commands import command_error
    from turboadb.results import OperationResult

    refused = "the device rejected it"
    assert command_error(OperationResult(True, "keyevent", True), refused) is None
    assert command_error(OperationResult(True, "keyevent", "done"), refused) is None
    assert command_error(OperationResult(True, "keyevent", False), refused) == refused
    assert command_error(OperationResult(False, "keyevent", None, None), refused) == refused
    assert (
        command_error(OperationResult(False, "keyevent", None, OSError("device offline")), refused)
        == "device offline"
    )
    assert command_error(False, refused) == refused  # a raw result
    assert command_error(None, refused) is None


class _RunNow:
    """Runs each submitted command at once and reports its result."""

    submitted = 0

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        self.submitted += 1
        result = fn()
        if on_done:
            on_done(result)
        return True

    def stop(self):
        pass


def test_the_screen_and_a_displays_buttons_report_a_failure_alike(qapp):
    from turboadb.gui.display_controls import DisplayControls
    from turboadb.gui.screencap_view import ScreencapView
    from turboadb.results import OperationResult

    class Handler:
        serial = "ivi"
        config = None

        def keyevent(self, *_args, **_kwargs):
            return OperationResult(False, "keyevent", None, None)  # failed, no error text

    buttons = DisplayControls(Handler(), 2, _RunNow())
    view = ScreencapView(Handler(), {"id": 2}, dispatcher=_RunNow())
    logs = []
    buttons.log.connect(logs.append)
    view.log.connect(logs.append)
    try:
        buttons.send_key("back", "Back")
        view._key(4, "back")
        assert logs == [
            "[WARNING] display 2 Back: the device rejected it",
            "[WARNING] screen input (back): the device rejected it",
        ]
    finally:
        view.close_view()
        view.deleteLater()
        buttons.deleteLater()
        qapp.processEvents()


def test_a_screen_view_closed_before_any_input_leaves_no_thread_behind(qapp):
    from turboadb.gui import qtutil
    from turboadb.gui.screencap_view import ScreencapView

    view = ScreencapView(object(), {"id": 0})  # its own dispatcher, never used
    dispatcher = view._dispatcher
    try:
        view.close_view()
        assert dispatcher not in qtutil._parked
    finally:
        view.deleteLater()
        qapp.processEvents()

    view = ScreencapView(object(), {"id": 0})
    dispatcher = view._dispatcher
    dispatcher.submit(lambda: True)  # input was sent: its thread runs
    try:
        view.close_view()
        assert dispatcher.wait(5000)  # and ends once the view has closed
    finally:
        view.deleteLater()
        qapp.processEvents()
