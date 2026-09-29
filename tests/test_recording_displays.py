"""Recording one display of a multi-display device.

- A display tile's recorder takes no audio, as its screen never does: with the
  default "output" source, recording a phone's second display muted the phone
  for the whole recording and put the whole device's sound in the MP4.
- Recording another display from the display manager records it beside the
  running screen, so the picker and the screen's shape stay with what is shown.

Headless; no device, adb or scrcpy is used.
"""

import pytest

pytest.importorskip("PyQt5")


class _Handler:
    serial = "phone"
    config = None


class _NullDispatcher:
    submitted = 0

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        return True

    def stop(self):
        pass


class _Session:
    """A running scrcpy screen."""

    log_path = None
    running = True

    def read_log(self):
        return ""

    def stop(self, timeout=None):
        self.running = False


@pytest.fixture
def panel(qapp):
    from turboadb.gui import settings as settings_mod
    from turboadb.gui.mirror_panel import MirrorPanel

    settings_mod.update({"scrcpy_audio": True, "scrcpy_audio_source": "output"})
    panel = MirrorPanel(_Handler(), {"name": "phone"}, automotive=False, dispatcher=_NullDispatcher())
    yield panel
    panel._scrcpy = None
    panel.close_panel()
    panel.close()


def test_a_display_tiles_recorder_takes_no_audio(panel):
    # Device Control's own screen records the device's sound, as chosen
    opts = panel._parallel_record_options("screen.mp4", None)
    assert opts.no_audio is False and opts.audio_source == "output"

    panel.set_fixed_display({"id": 2, "size": "1920x1080", "name": "HDMI Screen"})
    opts = panel._parallel_record_options("display2.mp4", 2)
    assert opts.no_audio is True
    assert opts.display_id == 2 and opts.record == "display2.mp4"


def test_recording_another_display_keeps_the_running_screens_display(panel):
    panel._got_displays(
        [{"id": 0, "size": "1080x2400", "name": "Built-in"}, {"id": 2, "size": "1920x1080"}]
    )
    begun = []
    panel._begin_record = lambda display_id="__use_combo__": begun.append(display_id)
    panel._scrcpy = _Session()  # display 0 is on screen
    panel._video_aspect = 1080 / 2400
    panel._select_and_record(2)  # the display manager's Record for display 2
    assert begun == [2]
    assert panel.cmb_display.currentData() is None  # still display 0
    assert panel._video_aspect == 1080 / 2400

    panel._scrcpy = None  # nothing on screen: Record selects the display too
    panel._select_and_record(2)
    assert begun == [2, 2] and panel.cmb_display.currentData() == 2
