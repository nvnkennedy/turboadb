"""Toast notifications (every message shown, sized to its text, kept on
screen) and saving connected devices as targets automatically.

Headless (Qt offscreen platform); no device, adb or network is used, and the
settings and saved-target files live in each test's temporary directory."""

from __future__ import annotations

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytest.importorskip("PyQt5")

_BIG = 1 << 20


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _fake_window(host, status=None, *, dock_visible=False, silent=False):
    """Just enough of MainWindow for ``MainWindow._log`` to run against a real
    host window (the toasts attach to it)."""
    from turboadb.gui.main_window import MainWindow

    status = [] if status is None else status
    return types.SimpleNamespace(
        window=lambda: host,
        log_panel=types.SimpleNamespace(
            append=lambda _text: None,
            chk_silent=types.SimpleNamespace(isChecked=lambda: silent),
        ),
        _log_dock=types.SimpleNamespace(isVisible=lambda: dock_visible),
        _LOG_LEVEL_RE=MainWindow._LOG_LEVEL_RE,
        _LEVEL_ALIASES=MainWindow._LEVEL_ALIASES,
        _show_log_dock=lambda: None,
        statusBar=lambda: types.SimpleNamespace(
            showMessage=lambda text, _ms: status.append(text)
        ),
    )


@pytest.fixture
def host(qapp, monkeypatch):
    """A shown window well inside the offscreen screen, with quiet beeps."""
    from PyQt5.QtWidgets import QLabel, QMainWindow

    from turboadb.gui import fileutil

    beeps = []
    monkeypatch.setattr(fileutil, "_play_error_sound", lambda: beeps.append(1))
    monkeypatch.setattr(fileutil, "_LAST_ERROR_SOUND", [float("-inf")])
    window = QMainWindow()
    window.setCentralWidget(QLabel("host"))
    window.resize(700, 500)
    window.move(20, 20)
    window.show()
    window.beeps = beeps
    qapp.processEvents()
    yield window
    for name in ("_turboadb_error_toast", "_turboadb_activity_toast"):
        toast = getattr(window, name, None)
        if toast is not None:
            toast.hide()
    window.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.fixture
def themed(qapp, monkeypatch):
    """Apply a theme for the test and put the previous stylesheet back after."""
    from turboadb.gui import theme

    monkeypatch.setattr(theme, "_ACTIVE_NAME", theme._ACTIVE_NAME)
    previous = qapp.styleSheet()

    def apply(name, extra=""):
        theme.apply_to_app(qapp, name)
        if extra:
            qapp.setStyleSheet(qapp.styleSheet() + extra)
        return name

    yield apply
    qapp.setStyleSheet(previous)


def _assert_nothing_clipped(label):
    """Every wrapped line of *label* fits inside it, and it inside its toast."""
    from PyQt5.QtCore import QRect, Qt

    flags = int(Qt.AlignLeft | Qt.TextWordWrap)
    needed = label.fontMetrics().boundingRect(QRect(0, 0, label.width(), _BIG), flags, label.text())
    assert needed.width() <= label.width(), (needed, label.geometry())
    assert needed.height() <= label.height(), (needed, label.geometry())
    assert label.height() >= label.heightForWidth(label.width())
    toast = label.window()
    assert toast.rect().contains(label.geometry()), (toast.rect(), label.geometry())


def _natural_width(label) -> int:
    """The widest line of *label*'s text with unlimited room."""
    from PyQt5.QtCore import QRect, Qt

    flags = int(Qt.AlignLeft | Qt.TextWordWrap)
    return label.fontMetrics().boundingRect(QRect(0, 0, _BIG, _BIG), flags, label.text()).width()


def _assert_sized_to_text(toast, host):
    """The toast rule, whatever the fonts measure: below the widest allowed
    width the text keeps its own lines at its own width; only text wider than
    that wraps, and then it fills the widest width. Nothing is ever clipped.

    (Font metrics differ between machines — without installed fonts Qt draws
    13 px boxes — so the rule is checked rather than fixed line counts.)"""
    from turboadb.gui import fileutil

    label = toast.text
    widest = round(fileutil._ACTIVITY_TEXT_MAX * fileutil._font_scale(label))
    room = host.width() - 2 * fileutil._TOAST_SIDE  # a roomy window, fully on screen
    at_limit = label.width() >= widest or toast.width() >= room
    natural = _natural_width(label)
    if at_limit:
        assert natural > label.width() - 8, (natural, label.width())
    else:
        assert label.heightForWidth(label.width()) == label.heightForWidth(_BIG)
        assert label.width() <= natural + 8, (natural, label.width())
    _assert_nothing_clipped(label)
    return at_limit


def _lines(label) -> int:
    from PyQt5.QtCore import QRect, Qt

    flags = int(Qt.AlignLeft | Qt.TextWordWrap)
    height = label.fontMetrics().boundingRect(QRect(0, 0, label.width(), _BIG), flags,
                                              label.text()).height()
    return round(height / label.fontMetrics().lineSpacing())


def _show_activity(qapp, host, message, **kwargs):
    """Show *message* in a fresh activity toast: hidden first, so neither a
    held warning nor a burst of updates (which only grows the toast) carries
    over from the previous message."""
    from turboadb.gui import fileutil

    previous = getattr(host, "_turboadb_activity_toast", None)
    if previous is not None:
        previous.hide()
    toast = fileutil.activity_toast(host, message, **kwargs)
    qapp.processEvents()
    return toast


def _screen_work(qapp):
    return qapp.primaryScreen().availableGeometry()


# --------------------------------------------------------------------------- #
# every message reaches its toast
# --------------------------------------------------------------------------- #
def test_every_message_reaches_its_toast_without_a_time_limit(qapp, monkeypatch):
    """Wi-Fi On then Bluetooth Off (within 4 s) used to show only the first."""
    from turboadb.gui import fileutil
    from turboadb.gui.main_window import MainWindow

    shown = []
    monkeypatch.setattr(
        fileutil, "activity_toast",
        lambda _parent, message, **kw: shown.append((kw.get("level"), message)),
    )
    monkeypatch.setattr(
        fileutil, "error_toast", lambda _parent, message, **_kw: shown.append(("error", message))
    )
    fake = _fake_window(None)
    for text in ("[OK] Wi-Fi on", "[OK] Bluetooth off", "[WARNING] slow", "[WARNING] slower",
                 "[ERROR] one", "[ERROR] two", "[CANCELLED] pull a.txt", "[DEBUG] noise",
                 "$ adb shell ls"):
        MainWindow._log(fake, text)
    assert shown == [
        ("ok", "Wi-Fi on"), ("ok", "Bluetooth off"), ("warning", "slow"), ("warning", "slower"),
        ("error", "one"), ("error", "two"), ("info", "Cancelled: pull a.txt"),
    ]


def test_two_quick_messages_both_reach_the_one_activity_toast(qapp, host):
    from PyQt5.QtWidgets import QFrame

    from turboadb.gui.main_window import MainWindow

    status = []
    fake = _fake_window(host, status)
    MainWindow._log(fake, "[OK] Wi-Fi on")
    toast = host._turboadb_activity_toast
    assert toast.text.text() == "Wi-Fi on" and not toast.isHidden()
    MainWindow._log(fake, "[OK] Bluetooth off")
    assert host._turboadb_activity_toast is toast
    assert toast.text.text() == "Bluetooth off" and not toast.isHidden()
    assert len(host.findChildren(QFrame, "activityToast")) == 1
    assert status == ["Wi-Fi on", "Bluetooth off"]


def test_a_burst_of_errors_is_one_counted_toast_and_one_beep(qapp, host):
    from PyQt5.QtWidgets import QFrame

    from turboadb.gui.main_window import MainWindow

    fake = _fake_window(host)
    for index in range(5):
        MainWindow._log(fake, f"[ERROR] adb pull failed: file {index}")
    toast = host._turboadb_error_toast
    assert len(host.findChildren(QFrame, "errorToast")) == 1
    assert toast.title.text() == "Error (5)"
    assert toast.text.text() == toast.message == "adb pull failed: file 4"
    assert host.beeps == [1]
    toast.hide()  # dismissed: the next failure starts a new count
    MainWindow._log(fake, "[ERROR] adb push failed")
    assert toast.title.text() == "Error"


def test_silent_with_the_log_open_mutes_the_popups(qapp, host):
    from turboadb.gui.main_window import MainWindow

    status = []
    MainWindow._log(_fake_window(host, status, dock_visible=True, silent=True), "[OK] Wi-Fi on")
    MainWindow._log(_fake_window(host, status, dock_visible=True, silent=True), "[ERROR] boom")
    assert getattr(host, "_turboadb_activity_toast", None) is None
    assert getattr(host, "_turboadb_error_toast", None) is None
    assert status == ["Wi-Fi on", "Error: boom"]
    # Silent only applies while the log is on screen.
    MainWindow._log(_fake_window(host, status, dock_visible=False, silent=True), "[OK] Wi-Fi off")
    assert host._turboadb_activity_toast.text.text() == "Wi-Fi off"


# --------------------------------------------------------------------------- #
# the activity toast is sized to its text
# --------------------------------------------------------------------------- #
def test_short_message_is_one_line_and_not_clipped_in_every_theme(qapp, host, themed):
    from turboadb.gui import theme

    for name in theme.theme_names():
        themed(name)
        toast = _show_activity(qapp, host, "Wi-Fi on", level="ok")
        label = toast.text
        assert _lines(label) == 1, name
        assert label.heightForWidth(label.width()) == label.heightForWidth(_BIG), name
        _assert_nothing_clipped(label)
        # sized to the text, not to a fixed column
        assert label.width() <= label.fontMetrics().horizontalAdvance("Wi-Fi on") + 8, name
        assert toast.width() < 260, name


def test_one_line_message_uses_its_full_width_instead_of_a_narrow_column(qapp, host, themed):
    """This message used to wrap at ~219 px although 460 px were allowed."""
    themed("dark")
    message = "Screen: scrcpy launched on display 0 · 1080x2400 · 16M · 60 fps"
    toast = _show_activity(qapp, host, message)
    if not _assert_sized_to_text(toast, host):
        assert _lines(toast.text) == 1


def test_long_message_wraps_within_the_maximum_width(qapp, host, themed):
    from turboadb.gui import fileutil

    themed("light")
    message = ("Pulled 42 item(s) to C:/Users/someone/Downloads/device-files/DCIM/Camera — every "
               "file was copied and verified against the device listing without any errors")
    toast = _show_activity(qapp, host, message, level="ok")
    label = toast.text
    widest = round(fileutil._ACTIVITY_TEXT_MAX * fileutil._font_scale(label))
    assert label.width() <= widest
    assert _assert_sized_to_text(toast, host)  # wider than allowed: fills the width
    assert _lines(label) >= 2
    assert toast.height() > label.heightForWidth(label.width())


def test_multi_line_message_keeps_its_lines_and_fits(qapp, host, themed):
    themed("dark")
    toast = _show_activity(qapp, host, "Shell command finished\nexit 0 · 12 lines of output")
    label = toast.text
    assert not _assert_sized_to_text(toast, host)
    assert _lines(label) == 2


def test_warning_with_an_action_is_laid_out_in_one_row(qapp, host, themed):
    themed("dark")
    toast = _show_activity(
        qapp, host, "a custom adb path is set (C:/tools/adb.exe) and takes precedence",
        level="warning", action_text="Show log", action=lambda: None,
    )
    label, action = toast.text, toast.action
    assert action.isVisibleTo(toast)
    if not _assert_sized_to_text(toast, host):
        assert _lines(label) == 1
    assert action.geometry().left() >= label.geometry().right()
    assert abs(action.geometry().center().y() - label.geometry().center().y()) <= 2
    assert toast.rect().contains(action.geometry())
    # a short warning is a single row: one line of text beside its button
    toast = _show_activity(qapp, host, "Device scan failed", level="warning",
                           action_text="Show log", action=lambda: None)
    assert not _assert_sized_to_text(toast, host)
    assert _lines(toast.text) == 1
    assert toast.height() < 2 * toast.text.fontMetrics().lineSpacing() + 30
    assert abs(toast.action.geometry().center().y() - toast.text.geometry().center().y()) <= 2


def test_unbreakable_text_wraps_instead_of_being_cut_off(qapp, host, themed):
    from turboadb.gui import fileutil

    themed("dark")
    message = ("Saved C:\\Users\\someone\\Downloads\\turboadb_INSTALL_FAILED_VERSION_DOWNGRADE_"
               "com.example.application_20260916_101010_screenshot_with_a_long_name.png")
    toast = _show_activity(qapp, host, message, level="ok")
    assert toast.message == message
    assert toast.text.text().replace(fileutil._ZWSP, "") == message
    assert _lines(toast.text) >= 2
    _assert_nothing_clipped(toast.text)
    # ordinary words stay free of invisible characters (copying stays clean)
    toast = _show_activity(qapp, host, "Wi-Fi on")
    assert fileutil._ZWSP not in toast.text.text()


@pytest.mark.parametrize("scale, point_size", [(1.25, 12), (1.5, 14)])
def test_larger_fonts_still_fit(qapp, host, themed, monkeypatch, scale, point_size):
    """125% / 150% Windows scaling: fonts grow while pixel sizes stay put."""
    from turboadb.gui import fileutil

    monkeypatch.setattr(fileutil, "_font_scale", lambda _widget: scale)
    themed("dark")
    normal = _show_activity(qapp, host, "Wi-Fi on").text.fontMetrics().height()
    themed("dark", "QLabel#activityToastText { font-size: %dpt; }"
                   " QLabel#errorToastText { font-size: %dpt; }" % (point_size, point_size))
    for message in ("Wi-Fi on", "Bluetooth off"):
        toast = _show_activity(qapp, host, message)
        assert toast.text.fontMetrics().height() > normal
        assert not _assert_sized_to_text(toast, host)
        assert _lines(toast.text) == 1
    toast = _show_activity(qapp, host, "Screen: scrcpy launched on display 0 · 1080x2400 · 16M · 60 fps"
                           " · audio forwarded", level="warning", action_text="Show log",
                           action=lambda: None)
    _assert_sized_to_text(toast, host)
    error = fileutil.error_toast(host, "adb: failed to install app.apk: Failure "
                                 "[INSTALL_FAILED_VERSION_DOWNGRADE] on the device", action=lambda: None)
    qapp.processEvents()
    _assert_nothing_clipped(error.text)


# --------------------------------------------------------------------------- #
# toasts stay on screen
# --------------------------------------------------------------------------- #
def test_toasts_stay_on_screen_near_an_edge_and_in_a_small_window(qapp, host, themed):
    from turboadb.gui import fileutil

    themed("dark")
    work = _screen_work(qapp)
    host.resize(260, 180)
    host.move(work.right() - 180, work.bottom() - 120)  # mostly off the bottom right
    qapp.processEvents()
    long_text = "Pulled 42 item(s) to the Downloads folder and verified every one of them"
    for message, kwargs in (("Wi-Fi on", {}), (long_text, {}),
                            ("Device scan failed", {"level": "warning", "action_text": "Show log",
                                                    "action": lambda: None})):
        toast = _show_activity(qapp, host, message, **kwargs)
        assert work.contains(toast.geometry()), (message, toast.geometry(), work)
        _assert_nothing_clipped(toast.text)
        # a narrow window must not squeeze the text into a sliver
        assert toast.text.width() >= min(
            toast.text.fontMetrics().horizontalAdvance(message), fileutil._ACTIVITY_TEXT_MIN
        )
    error = fileutil.error_toast(host, "adb: device offline while pulling a file", action=lambda: None)
    qapp.processEvents()
    assert work.contains(error.geometry())
    _assert_nothing_clipped(error.text)

    host.move(work.left() - 200, work.top() + 40)  # off the left edge
    qapp.processEvents()
    toast = _show_activity(qapp, host, long_text)
    assert work.contains(toast.geometry())


def test_toasts_sit_inside_a_roomy_window(qapp, host, themed):
    from PyQt5.QtCore import QPoint, QRect

    themed("dark")
    toast = _show_activity(qapp, host, "Wi-Fi on")
    frame = QRect(host.mapToGlobal(QPoint(0, 0)), host.size())
    assert frame.contains(toast.geometry())
    assert frame.right() - toast.geometry().right() <= 30  # anchored bottom right
    assert frame.bottom() - toast.geometry().bottom() <= 60


def test_activity_toast_moves_above_an_overlapping_error_toast(qapp, host, themed):
    from turboadb.gui import fileutil

    themed("dark")
    error = fileutil.error_toast(host, "adb: failed to install app.apk", action=lambda: None)
    toast = _show_activity(qapp, host, "Screen: scrcpy launched on display 0 · 1080x2400 · 16M")
    assert not toast.geometry().intersects(error.geometry())
    assert toast.geometry().bottom() < error.geometry().top()
    # an error arriving while the activity toast is up moves it out of the way too
    error.hide()
    toast = _show_activity(qapp, host, "Screen: scrcpy launched on display 0 · 1080x2400 · 16M")
    fileutil.error_toast(host, "adb: failed again", action=lambda: None)
    qapp.processEvents()
    assert not toast.geometry().intersects(error.geometry())


def test_saved_notification_error_and_activity_toasts_never_cover_each_other(qapp, host, themed):
    """A save reports twice ("[OK] logcat saved to …" and the Saved popup) in
    the same corner, and an error toast can be up too: the activity toast used
    to cover the popup, which covered the error toast's buttons."""
    from PyQt5.QtWidgets import QLabel

    from turboadb.gui import fileutil

    themed("dark")
    path = "C:\\Users\\someone\\Downloads\\logcat-20260916-101010.log"
    error = fileutil.error_toast(host, "adb: device offline", action=lambda: None)
    activity = _show_activity(qapp, host, f"logcat saved to {path}", level="ok")
    fileutil.saved_toast(host, path, "logcat")
    fileutil.saved_toast(host, path.replace("logcat-", "bugreport-"), "bugreport")
    qapp.processEvents()
    notes = [note for note in host._turboadb_toasts if note.isVisible()]
    try:
        assert len(notes) == 2
        shown = [error, activity] + notes
        work = _screen_work(qapp)
        for index, toast in enumerate(shown):
            assert work.contains(toast.geometry()), toast.objectName()
            for other in shown[index + 1:]:
                assert not toast.geometry().intersects(other.geometry()), (
                    toast.objectName(), other.objectName())
        # oldest notification lowest, the activity toast on top of the column
        assert notes[0].y() > notes[1].y() > activity.y()
        body = next(label for label in notes[0].findChildren(QLabel)
                    if label.objectName() == "notificationToastBody")
        assert body.heightForWidth(body.width()) <= body.height()
    finally:
        for note in notes:
            note.close()
        qapp.processEvents()


def test_error_copy_button_copies_the_message_as_given(qapp, host):
    from PyQt5.QtWidgets import QApplication, QPushButton

    from turboadb.gui import fileutil

    message = "adb: " + "x" * 300
    error = fileutil.error_toast(host, message, action=lambda: None)
    copy = next(b for b in error.findChildren(QPushButton) if b.text() == "Copy")
    copy.click()
    assert QApplication.clipboard().text() == message


# --------------------------------------------------------------------------- #
# connected devices are saved as targets
# --------------------------------------------------------------------------- #
@pytest.fixture
def target_files(monkeypatch, tmp_path):
    from turboadb.gui import sessions, settings

    monkeypatch.setattr(settings, "_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(sessions, "_FILE", str(tmp_path / "sessions.json"))
    return tmp_path


def _write_targets(path, targets):
    with open(path / "sessions.json", "w", encoding="utf-8") as fh:
        json.dump(targets, fh)


def _fake_main(store=None):
    """The parts of MainWindow the auto-save uses, recording what it does."""
    from turboadb.gui.main_window import MainWindow
    from turboadb.gui.sessions import SessionStore

    fake = types.SimpleNamespace(
        _closing=False,
        store=store or SessionStore(),
        logged=[],
        refreshed=[],
        _PLACEHOLDER_NAMES=MainWindow._PLACEHOLDER_NAMES,
    )
    fake._log = fake.logged.append
    fake.refresh_sessions = lambda: fake.refreshed.append(True)
    fake._connected_target = lambda tab: MainWindow._connected_target(fake, tab)
    return fake


def _tab(session, serial, title):
    from turboadb.gui.sessions import normalize_session

    return types.SimpleNamespace(
        session=normalize_session(session),
        handler=types.SimpleNamespace(serial=serial),
        title_label=types.SimpleNamespace(text=lambda: title),
        _session_closed=False,
    )


def _saved(path):
    with open(path / "sessions.json", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize(
    "session, serial, title, expected",
    [
        ({"name": "V2318", "type": "usb", "serial": "10AD5F1E2B"}, "10AD5F1E2B", "vivo V2318",
         {"name": "vivo V2318", "type": "usb", "serial": "10AD5F1E2B"}),
        ({"name": "192.168.1.7:5555", "type": "network", "host": "192.168.1.7", "port": 5555},
         "192.168.1.7:5555", "192.168.1.7:5555",
         {"name": "192.168.1.7:5555", "type": "network", "host": "192.168.1.7", "port": 5555}),
        ({"name": "lab-pc", "type": "remote", "adb_host": "lab-pc", "adb_port": 5037, "serial": ""},
         "R58M123", "lab-pc",
         {"name": "R58M123", "type": "remote", "adb_host": "lab-pc", "adb_port": 5037,
          "serial": "R58M123"}),
        ({"name": "Pixel", "type": "remote", "adb_host": "lab-pc", "adb_port": 5038,
          "serial": "R58M123"}, "R58M123", "Google Pixel 8",
         {"name": "Google Pixel 8", "type": "remote", "adb_host": "lab-pc", "adb_port": 5038,
          "serial": "R58M123"}),
        # an "only device" USB tab that turned out to be a Wi-Fi device
        ({"name": "device", "type": "usb", "serial": ""}, "10.0.0.5:5555", "device",
         {"name": "10.0.0.5:5555", "type": "network", "host": "10.0.0.5", "port": 5555}),
    ],
    ids=["usb", "network", "remote-only-device", "remote-serial", "usb-on-wifi"],
)
def test_a_connected_device_is_saved_once(qapp, target_files, session, serial, title, expected):
    from turboadb.gui.main_window import MainWindow

    fake = _fake_main()
    tab = _tab(session, serial, title)
    MainWindow._auto_save_target(fake, tab)
    assert _saved(target_files) == [expected]
    assert fake.logged == [f"[OK] Saved target '{expected['name']}'"]
    assert fake.refreshed == [True]
    assert fake.store.names() == [expected["name"]]
    # reconnecting (or the probe finishing) must not add it again
    MainWindow._auto_save_target(fake, tab)
    assert _saved(target_files) == [expected]
    assert fake.logged == [f"[OK] Saved target '{expected['name']}'"]


def test_opening_a_saved_target_adds_nothing(qapp, target_files):
    from turboadb.gui.main_window import MainWindow

    existing = [
        {"name": "My phone", "type": "usb", "serial": "10AD5F1E2B"},
        {"name": "Any phone", "type": "usb", "serial": ""},
        {"name": "Car", "type": "network", "host": "192.168.1.50", "port": 5555},
    ]
    _write_targets(target_files, existing)
    fake = _fake_main()
    # the same device under the name the user gave it
    MainWindow._auto_save_target(fake, _tab(existing[0], "10AD5F1E2B", "vivo V2318"))
    # the "only device" target resolves to a serial once connected
    MainWindow._auto_save_target(fake, _tab(existing[1], "ZZ99", "vivo V2318"))
    # the same network device, host written differently
    MainWindow._auto_save_target(
        fake, _tab({"name": "x", "type": "network", "host": "192.168.1.50", "port": "5555"},
                   "192.168.1.50:5555", "Head unit")
    )
    assert _saved(target_files) == existing
    assert fake.logged == [] and fake.refreshed == []


def test_a_target_saved_by_another_window_is_not_duplicated(qapp, target_files):
    from turboadb.gui.main_window import MainWindow
    from turboadb.gui.sessions import SessionStore

    first, second = _fake_main(SessionStore()), _fake_main(SessionStore())
    tab = _tab({"name": "V2318", "type": "usb", "serial": "10AD5F1E2B"}, "10AD5F1E2B", "vivo V2318")
    MainWindow._auto_save_target(first, tab)
    MainWindow._auto_save_target(second, tab)  # its store was loaded before the save
    assert [t["name"] for t in _saved(target_files)] == ["vivo V2318"]
    assert second.logged == []


def test_names_are_made_unique_without_replacing_another_device(qapp, target_files):
    from turboadb.gui.main_window import MainWindow

    other = {"name": "vivo V2318", "type": "usb", "serial": "OTHER"}
    _write_targets(target_files, [other, dict(other, name="vivo V2318 (2)", serial="THIRD")])
    fake = _fake_main()
    MainWindow._auto_save_target(
        fake, _tab({"name": "V2318", "type": "usb", "serial": "10AD5F1E2B"}, "10AD5F1E2B",
                   "vivo V2318")
    )
    saved = _saved(target_files)
    assert saved[:2] == [other, dict(other, name="vivo V2318 (2)", serial="THIRD")]
    assert saved[2] == {"name": "vivo V2318 (3)", "type": "usb", "serial": "10AD5F1E2B"}
    assert fake.logged == ["[OK] Saved target 'vivo V2318 (3)'"]


def test_a_placeholder_title_names_the_target_by_its_address(qapp, target_files):
    from turboadb.gui.main_window import MainWindow

    fake = _fake_main()
    MainWindow._auto_save_target(fake, _tab({"name": "device", "type": "usb"}, "ABC123", "device"))
    MainWindow._auto_save_target(
        fake, _tab({"name": "fe80::1", "type": "network", "host": "fe80::1", "port": 5555},
                   "[fe80::1]:5555", "")
    )
    assert [t["name"] for t in _saved(target_files)] == ["ABC123", "[fe80::1]:5555"]


def test_nothing_is_saved_with_the_setting_off(qapp, target_files):
    from turboadb.gui import settings
    from turboadb.gui.main_window import MainWindow

    assert settings.get("auto_save_targets") is True  # on by default
    settings.set("auto_save_targets", False)
    fake = _fake_main()
    MainWindow._auto_save_target(
        fake, _tab({"name": "V2318", "type": "usb", "serial": "10AD5F1E2B"}, "10AD5F1E2B", "vivo")
    )
    assert not (target_files / "sessions.json").exists()
    assert fake.logged == [] and fake.refreshed == []


def test_closed_or_unconnected_tabs_are_not_saved(qapp, target_files):
    from turboadb.gui.main_window import MainWindow

    fake = _fake_main()
    tab = _tab({"name": "V2318", "type": "usb", "serial": "10AD5F1E2B"}, "10AD5F1E2B", "vivo")
    tab._session_closed = True
    MainWindow._auto_save_target(fake, tab)
    tab._session_closed, tab.handler = False, None
    MainWindow._auto_save_target(fake, tab)
    fake._closing = True
    MainWindow._auto_save_target(fake, _tab({"name": "a", "serial": "B"}, "B", "b"))
    assert not (target_files / "sessions.json").exists() and fake.logged == []


def test_a_failed_auto_save_is_a_warning_and_leaves_the_file_alone(qapp, target_files):
    from turboadb.gui.main_window import MainWindow

    broken = "[{not json"
    (target_files / "sessions.json").write_text(broken, encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        fake = _fake_main()
    MainWindow._auto_save_target(
        fake, _tab({"name": "V2318", "type": "usb", "serial": "10AD5F1E2B"}, "10AD5F1E2B", "vivo")
    )
    assert (target_files / "sessions.json").read_text(encoding="utf-8") == broken
    assert len(fake.logged) == 1 and fake.logged[0].startswith("[WARNING] Could not add 'vivo'")
    assert fake.refreshed == []


def test_the_sidebar_lists_the_saved_target(qapp, target_files):
    from PyQt5.QtWidgets import QLabel, QLineEdit, QListWidget

    from turboadb.gui.main_window import MainWindow

    fake = _fake_main()
    fake.session_list, fake.quick, fake._saved_empty = QListWidget(), QLineEdit(), QLabel()
    fake._TYPE_ICON = MainWindow._TYPE_ICON
    fake._filter_sessions = lambda text: MainWindow._filter_sessions(fake, text)
    fake.refresh_sessions = lambda: MainWindow.refresh_sessions(fake)
    try:
        MainWindow._auto_save_target(
            fake, _tab({"name": "V2318", "type": "usb", "serial": "10AD5F1E2B"}, "10AD5F1E2B",
                       "vivo V2318")
        )
        assert fake.session_list.count() == 1
        assert "vivo V2318" in fake.session_list.item(0).text()
        assert fake.session_list.item(0).data(0x0100) == "vivo V2318"  # Qt.UserRole
    finally:
        for widget in (fake.session_list, fake.quick, fake._saved_empty):
            widget.deleteLater()


def test_the_device_tab_connection_triggers_one_auto_save(qapp, target_files):
    """The hook in _add_device_tab: DeviceTab.connected (once per tab) saves the
    device; log lines alone never count as a connection."""
    from turboadb.gui.device_tab import DeviceTab
    from turboadb.gui.main_window import MainWindow

    tab = DeviceTab({"name": "V2318", "serial": "10AD5F1E2B"})
    tab._started_connect = True
    calls = []
    fake = types.SimpleNamespace(_auto_save_target=calls.append)
    try:
        MainWindow._watch_connection(fake, tab)
        tab.log.emit("[OK] V2318: connected")
        qapp.processEvents()
        assert calls == []
        tab.handler = types.SimpleNamespace(serial="10AD5F1E2B")
        tab._announce_connected()
        tab._announce_connected()  # a reconnect is not a new connection
        qapp.processEvents()
        assert calls == [tab]
    finally:
        tab.handler = None
        tab.close_session()
        tab.close()
        tab.deleteLater()
        qapp.processEvents()


def test_add_device_tab_watches_the_connection():
    import inspect

    from turboadb.gui.main_window import MainWindow

    body = inspect.getsource(MainWindow._add_device_tab)
    assert "self._watch_connection(w)" in body


# --------------------------------------------------------------------------- #
# the setting
# --------------------------------------------------------------------------- #
def test_settings_checkbox_saves_on_ok_and_discards_on_cancel(qapp, target_files, themed):
    from turboadb.gui import settings
    from turboadb.gui.settings_dialog import SettingsDialog

    themed("dark")
    assert settings.DEFAULTS["auto_save_targets"] is True
    dlg = SettingsDialog()
    try:
        assert dlg.auto_save_targets.isChecked()
        dlg.auto_save_targets.setChecked(False)
        assert dlg.changed_settings() == {"auto_save_targets": False}
        dlg.reject()
    finally:
        dlg.deleteLater()
    assert settings.get("auto_save_targets") is True  # Cancel discarded it

    dlg = SettingsDialog()
    try:
        dlg.auto_save_targets.setChecked(False)
        dlg.accept()
    finally:
        dlg.deleteLater()
    assert settings.get("auto_save_targets") is False

    dlg = SettingsDialog()
    try:
        assert not dlg.auto_save_targets.isChecked()
        assert dlg.changed_settings() == {}
        dlg.auto_save_targets.setChecked(True)
        dlg.accept()
    finally:
        dlg.deleteLater()
    assert settings.get("auto_save_targets") is True


def test_session_store_identity_helpers(target_files):
    from turboadb.gui.sessions import SessionStore, session_identity

    assert session_identity({"type": "network", "host": "[FE80::1]", "port": 5555}) == (
        "network", "fe80::1", "5555")
    assert session_identity({"type": "remote", "adb_host": "Lab-PC", "serial": "X"}) == (
        "remote", "lab-pc", "5037", "X")
    assert session_identity({"serial": "abc"}) == ("usb", "abc")
    assert session_identity(None) is None
    store = SessionStore()
    assert store.add_if_new({"name": "a", "serial": "S1"})["name"] == "a"
    assert store.add_if_new({"name": "b", "serial": "S1"}) is None
    assert store.add_if_new({"name": "A", "serial": "S2"})["name"] == "A (2)"
    assert store.add_if_new({"name": "c", "serial": "S3"}, also=[("usb", "S2")]) is None
    assert store.find_identity(("usb", "S2"))["name"] == "A (2)"
    with pytest.raises(ValueError):
        store.add_if_new({"name": "", "serial": "S4"})


# --------------------------------------------------------------------------- #
# review fixes
# --------------------------------------------------------------------------- #
ZWSP = chr(0x200B)


@pytest.mark.parametrize("line, text", [
    ("[OK] Home", "Home"), ("[OK] Notifications", "Notifications"),
    ("[INFO] Battery…", "Battery…"), ("192.168.1.7:5555", "192.168.1.7:5555"),
    ("[OK] com.android.settings", "com.android.settings"),
])
def test_a_one_word_message_is_not_an_empty_toast(qapp, host, themed, line, text):
    """A word without a break point keeps its height at any width, so the
    width search ran down to 1 px: every Device Control key press ("[OK] Home")
    showed an empty toast."""
    from turboadb.gui.main_window import MainWindow

    themed("dark")
    MainWindow._log(_fake_window(host), line)
    qapp.processEvents()
    toast = host._turboadb_activity_toast
    label = toast.text
    assert label.text() == text
    assert label.width() >= label.fontMetrics().horizontalAdvance(text)
    assert _lines(label) == 1
    _assert_nothing_clipped(label)


def test_a_short_saved_path_is_not_squeezed(qapp, host, themed):
    from PyQt5.QtCore import QRect, Qt
    from PyQt5.QtWidgets import QLabel

    from turboadb.gui import fileutil

    themed("dark")
    path = "C:" + chr(92) + "Temp" + chr(92) + "shot.png"
    fileutil.saved_toast(host, path, "screenshot")
    qapp.processEvents()
    note = host._turboadb_toasts[-1]
    try:
        body = next(label for label in note.findChildren(QLabel)
                    if label.objectName() == "notificationToastBody")
        plain = fileutil._plain_text(body.text())
        flags = int(Qt.AlignLeft | Qt.TextWordWrap)
        needed = body.fontMetrics().boundingRect(QRect(0, 0, body.width(), _BIG), flags, plain)
        assert needed.width() <= body.width()
        assert body.width() >= body.fontMetrics().horizontalAdvance(path)
    finally:
        note.close()
        qapp.processEvents()


def _stand_in_tab_class(with_trace):
    """A QWidget with DeviceTab's signals, with or without ``trace``."""
    from PyQt5.QtCore import pyqtSignal
    from PyQt5.QtWidgets import QWidget

    class Tab(QWidget):
        log = pyqtSignal(str)
        title_changed = pyqtSignal(str)
        screen_active = pyqtSignal(bool)
        connected = pyqtSignal()
        if with_trace:
            trace = pyqtSignal(str)

        def __init__(self, session, parent=None, *, terminal_only=False):
            super().__init__(parent)
            self.session = session
            self._terminal_only = terminal_only
            self.handler = None
            self.connecting = False

        def start_connect(self):
            self.connecting = True

    return Tab


@pytest.mark.parametrize("with_trace", [True, False])
def test_engine_trace_lines_reach_the_log_only(qapp, monkeypatch, with_trace):
    """The engine's own "[ERROR] uninstall failed" plus the page's report of
    the same failure showed "Error (2)" for one failed action."""
    from PyQt5.QtWidgets import QTabWidget

    import turboadb.gui.main_window as mw_mod
    from turboadb.gui.main_window import MainWindow

    monkeypatch.setattr(mw_mod, "DeviceTab", _stand_in_tab_class(with_trace))
    panel, logged, status = [], [], []
    fake = types.SimpleNamespace(
        tabs=QTabWidget(),
        log_panel=types.SimpleNamespace(append=panel.append),
        statusBar=lambda: types.SimpleNamespace(showMessage=lambda *a: status.append(a)),
        _log=logged.append,
        _on_screen_active=lambda _active: None,
        _set_tab_title=lambda _widget, _title: None,
        _update_center=lambda: None,
    )
    fake._log_trace = lambda text: MainWindow._log_trace(fake, text)
    fake._watch_connection = lambda tab: MainWindow._watch_connection(fake, tab)
    try:
        MainWindow._add_device_tab(fake, {"name": "Pixel", "type": "usb", "serial": "S1"}, "Pixel")
        tab = fake.tabs.widget(0)
        assert tab.connecting
        logged.clear()
        tab.log.emit("[ERROR] uninstall: Failure [DELETE_FAILED_INTERNAL_ERROR]")
        assert logged == ["[ERROR] uninstall: Failure [DELETE_FAILED_INTERNAL_ERROR]"]
        if with_trace:
            tab.trace.emit("[ERROR] uninstall failed: Failure [DELETE_FAILED_INTERNAL_ERROR]")
            tab.trace.emit("$ adb -s S1 uninstall com.example")
            assert panel == ["[ERROR] uninstall failed: Failure [DELETE_FAILED_INTERNAL_ERROR]",
                             "$ adb -s S1 uninstall com.example"]
            assert logged == ["[ERROR] uninstall: Failure [DELETE_FAILED_INTERNAL_ERROR]"]
            assert status == []
    finally:
        fake.tabs.deleteLater()
        qapp.processEvents()


def test_log_trace_never_toasts(qapp, host):
    from turboadb.gui.main_window import MainWindow

    fake = _fake_window(host)
    lines = []
    fake.log_panel = types.SimpleNamespace(append=lines.append)
    MainWindow._log_trace(fake, "[ERROR] connect_tcp failed: refused")
    assert lines == ["[ERROR] connect_tcp failed: refused"]
    assert getattr(host, "_turboadb_error_toast", None) is None
    assert getattr(host, "_turboadb_activity_toast", None) is None


def test_auto_save_does_not_bring_back_deleted_or_renamed_targets(qapp, target_files):
    from turboadb.gui.main_window import MainWindow
    from turboadb.gui.sessions import SessionStore

    _write_targets(target_files, [
        {"name": "Pixel", "type": "usb", "serial": "S1"},
        {"name": "Old car", "type": "network", "host": "10.0.0.9", "port": 5555},
    ])
    window = _fake_main(SessionStore())  # started before the edits below
    other = SessionStore()  # the CLI or another window
    other.delete("Old car")
    other.save({"name": "My Pixel", "type": "usb", "serial": "S1", "previous_name": "Pixel"})
    assert [t["name"] for t in _saved(target_files)] == ["My Pixel"]
    MainWindow._auto_save_target(
        window, _tab({"name": "V2318", "type": "usb", "serial": "S2"}, "S2", "vivo V2318"))
    assert _saved(target_files) == [
        {"name": "My Pixel", "type": "usb", "serial": "S1"},
        {"name": "vivo V2318", "type": "usb", "serial": "S2"},
    ]
    assert window.store.names() == ["My Pixel", "vivo V2318"]
    # the renamed target is still known by its device, under its new name
    MainWindow._auto_save_target(
        window, _tab({"name": "Pixel", "type": "usb", "serial": "S1"}, "S1", "Google Pixel"))
    assert len(_saved(target_files)) == 2


def test_a_fresh_warning_is_not_replaced_by_the_next_info(qapp, host, monkeypatch):
    from PyQt5.QtTest import QTest

    from turboadb.gui import fileutil
    from turboadb.gui.main_window import MainWindow

    monkeypatch.setattr(fileutil, "_WARNING_HOLD_S", 0.3)
    status = []
    fake = _fake_window(host, status)
    MainWindow._log(fake, "[WARNING] scrcpy: download failed: HTTP Error 404")
    MainWindow._log(fake, "Pulling 1 item(s) to the Downloads folder…")
    MainWindow._log(fake, "[OK] Tools ready: adb")
    toast = host._turboadb_activity_toast
    assert toast.message == "scrcpy: download failed: HTTP Error 404"
    assert status[-1] == "Tools ready: adb"  # the status bar is never held back
    QTest.qWait(600)
    assert toast.message == "Tools ready: adb" and not toast.isHidden()
    # a newer warning replaces a warning at once, and drops the queued info
    MainWindow._log(fake, "[WARNING] first")
    MainWindow._log(fake, "[INFO] queued")
    MainWindow._log(fake, "[WARNING] second")
    assert toast.message == "second"
    QTest.qWait(600)
    assert toast.message == "second"


def test_tools_download_reports_one_outcome(qapp, monkeypatch):
    import turboadb.gui.main_window as mw_mod
    from turboadb.gui.main_window import MainWindow

    boxes = []
    monkeypatch.setattr(mw_mod.QMessageBox, "warning",
                        staticmethod(lambda *args, **kwargs: boxes.append(args[1])))
    logged, panel = [], []
    fake = types.SimpleNamespace(
        _dlg=types.SimpleNamespace(close=lambda: None), _start_poll_timer=lambda: None,
        log_panel=types.SimpleNamespace(append=panel.append), _log=logged.append,
        _begin_adb_startup=lambda: None,
    )
    MainWindow._tools_done(fake, {"adb": "C:/t/adb.exe", "scrcpy": None,
                                  "errors": {"scrcpy": "download failed: HTTP Error 404"}})
    assert logged == ["[WARNING] scrcpy: download failed: HTTP Error 404 (adb ready)"]
    assert boxes == []
    logged.clear()
    MainWindow._tools_done(fake, {"adb": None, "scrcpy": None, "errors": {"download": "offline"}})
    assert logged == ["[WARNING] download: offline"] and boxes == ["Download failed"]
    logged.clear()
    MainWindow._tools_done(fake, {"adb": "a", "scrcpy": "s"})
    assert logged == ["[OK] Tools ready: adb, scrcpy"]


def _context_copy(qapp, label, *, select_all):
    """Open *label*'s context menu, pick its Copy action, return the clipboard."""
    from PyQt5.QtCore import QPoint, QTimer
    from PyQt5.QtGui import QContextMenuEvent
    from PyQt5.QtWidgets import QApplication, QMenu

    if select_all:
        label.setSelection(0, len(label.text()))
    else:
        label.setSelection(0, 0)
    QApplication.clipboard().setText("before")
    actions = []

    def pick():
        for top in QApplication.topLevelWidgets():
            if isinstance(top, QMenu) and top.isVisible():
                actions.append([action.text() for action in top.actions()])
                next(a for a in top.actions() if a.text() == "Copy").trigger()
                top.close()

    QTimer.singleShot(30, pick)
    event = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), label.mapToGlobal(QPoint(5, 5)))
    QApplication.sendEvent(label, event)
    qapp.processEvents()
    assert actions == [["Copy", "Select All"]]
    return QApplication.clipboard().text()


def test_copying_toast_text_never_includes_the_break_points(qapp, host, themed):
    from PyQt5.QtWidgets import QLabel

    from turboadb.gui import fileutil

    themed("dark")
    sep = chr(92)
    path = sep.join(["C:", "Users", "someone", "Downloads",
                     "turboadb_INSTALL_FAILED_VERSION_DOWNGRADE_com.example.application_"
                     "20260916_101010_screenshot_with_a_long_name_and_more.png"])
    message = "adb: failed to pull " + path
    error = fileutil.error_toast(host, message, action=lambda: None)
    qapp.processEvents()
    assert ZWSP in error.text.text()  # the break points are there on screen...
    assert _context_copy(qapp, error.text, select_all=True) == message  # ...not in a copy
    assert _context_copy(qapp, error.text, select_all=False) == message
    fileutil.saved_toast(host, path, "screenshot")
    qapp.processEvents()
    note = host._turboadb_toasts[-1]
    try:
        body = next(label for label in note.findChildren(QLabel)
                    if label.objectName() == "notificationToastBody")
        copied = _context_copy(qapp, body, select_all=False)
        assert ZWSP not in copied and copied.splitlines()[-1] == path
    finally:
        note.close()
        qapp.processEvents()


def test_break_points_never_split_a_character(qapp):
    import unicodedata

    from PyQt5.QtGui import QFont, QFontMetrics

    from turboadb.gui import fileutil

    metrics = QFontMetrics(QFont("Segoe UI", 9))
    virama = chr(0x094D)
    zwj = chr(0x200D)
    hindi = "मेरेफ़ोनकीतस्वीरेंऔरवीडियोजोकलरिकॉर्डकीगईथींउनकीपूरीसूचीयहाँहै" * 2
    vietnamese = unicodedata.normalize("NFD", "Ảnhchụpmànhìnhđiệnthoạiđượclưuvàothưmục" * 2)
    family = zwj.join([chr(0x1F468), chr(0x1F469), chr(0x1F467), chr(0x1F466)]) * 6
    flags = (chr(0x1F1EE) + chr(0x1F1F3)) * 12
    path = "C:" + chr(92) + "a_b" * 40
    for word in (hindi, vietnamese, family, flags, path):
        broken = fileutil._breakable(word, metrics, 40)
        assert broken.replace(ZWSP, "") == word
        if word in (hindi, vietnamese, path):
            assert ZWSP in broken
        pieces = broken.split(ZWSP)
        for before, after in zip(pieces, pieces[1:]):
            assert unicodedata.category(after[0]) not in ("Mn", "Mc", "Me"), repr(after[:3])
            assert before[-1] != virama and after[0] != zwj and before[-1] != zwj
            assert not (0x1F1E6 <= ord(before[-1]) <= 0x1F1FF
                        and 0x1F1E6 <= ord(after[0]) <= 0x1F1FF)
    assert fileutil._breakable("Wi-Fi on", metrics, 400) == "Wi-Fi on"  # words that fit stay


def test_deploy_summary_counts_skipped_hosts(qapp, monkeypatch):
    import turboadb.gui.main_window as mw_mod
    from turboadb.gui.main_window import MainWindow

    boxes = []
    monkeypatch.setattr(mw_mod.QMessageBox, "warning",
                        staticmethod(lambda *args, **kwargs: boxes.append(("warning", args[2]))))
    monkeypatch.setattr(mw_mod.QMessageBox, "information",
                        staticmethod(lambda *args, **kwargs: boxes.append(("info", args[2]))))

    def run(messages, hosts):
        logged = []
        fake = types.SimpleNamespace(
            _dep_results=[], _dep_hosts=hosts, _dep_skipped=0, _log=logged.append,
            _dep_dlg=types.SimpleNamespace(setLabelText=lambda _t: None, close=lambda: None),
            log_panel=types.SimpleNamespace(append=lambda _t: None), _poll_devices=lambda: None,
        )
        for message in messages:
            MainWindow._on_deploy_status(fake, message)
        MainWindow._on_deploy_finished(fake)
        return logged

    logged = run(["[INFO] 2 host(s), 2 at a time (300s budget).", "[OK] a: serve started — ok",
                  "[WARNING] Skipped 1 host(s) — the 300s budget ran out: b",
                  "[OK] Remote deploy finished."], hosts=2)
    assert logged[-1] == "[WARNING] Remote deploy finished: 1 host(s) deployed, 1 skipped"
    assert "[WARNING] Skipped 1 host(s) — the 300s budget ran out: b" in logged
    assert boxes[-1][0] == "warning" and "Skipped 1 host(s)" in boxes[-1][1]
    # a host that reported nothing at all is not a success either
    logged = run(["[OK] a: serve started — ok", "[OK] Remote deploy finished."], hosts=2)
    assert logged[-1] == "[WARNING] Remote deploy finished: 1 host(s) deployed, 1 skipped"
    logged = run(["[OK] a: serve started — ok", "[ERROR] b: WinRM failed: timeout",
                  "[OK] Remote deploy finished."], hosts=2)
    assert logged[-1] == "[WARNING] Remote deploy finished: 1 host(s) deployed, 1 failed"
    logged = run(["[OK] a: serve started — ok", "[OK] b: serve started — ok",
                  "[OK] Remote deploy finished."], hosts=2)
    assert logged[-1] == "[OK] Remote deploy finished: 2 host(s) deployed"
    assert boxes[-1][0] == "info"


def test_a_new_wireless_debugging_port_updates_the_saved_target(qapp, target_files):
    from turboadb.gui.main_window import MainWindow

    fake = _fake_main()
    MainWindow._auto_save_target(
        fake, _tab({"name": "V2318", "type": "usb", "serial": "10AD5F1E2B"}, "10AD5F1E2B",
                   "vivo V2318"))
    for port in (5555, 37123, 41555):
        MainWindow._auto_save_target(
            fake, _tab({"name": "192.168.1.7", "type": "network", "host": "192.168.1.7",
                        "port": port}, f"192.168.1.7:{port}", "vivo V2318"))
    # another device that got the same address later is never merged into it
    MainWindow._auto_save_target(
        fake, _tab({"name": "192.168.1.7", "type": "network", "host": "192.168.1.7", "port": 5555},
                   "192.168.1.7:5555", "Head unit"))
    # an unnamed Connect -> Network tab is labelled with the bare host
    MainWindow._auto_save_target(
        fake, _tab({"name": "10.0.0.5", "type": "network", "host": "10.0.0.5", "port": 5555},
                   "10.0.0.5:5555", "10.0.0.5"))
    assert _saved(target_files) == [
        {"name": "vivo V2318", "type": "usb", "serial": "10AD5F1E2B"},
        {"name": "vivo V2318 (2)", "type": "network", "host": "192.168.1.7", "port": 41555},
        {"name": "Head unit", "type": "network", "host": "192.168.1.7", "port": 5555},
        {"name": "10.0.0.5:5555", "type": "network", "host": "10.0.0.5", "port": 5555},
    ]
    assert fake.logged == [
        "[OK] Saved target 'vivo V2318'",
        "[OK] Saved target 'vivo V2318 (2)'",
        "[OK] Saved target 'vivo V2318 (2)' now uses port 37123",
        "[OK] Saved target 'vivo V2318 (2)' now uses port 41555",
        "[OK] Saved target 'Head unit'",
        "[OK] Saved target '10.0.0.5:5555'",
    ]


def test_sustained_errors_beep_once_per_appearance(qapp, host, monkeypatch):
    from turboadb.gui import fileutil

    monkeypatch.setattr(fileutil, "_ERROR_BEEP_GAP_S", 0.0)  # only the appearance rule counts
    for index in range(12):
        fileutil.error_toast(host, f"connect_tcp failed: attempt {index}", action=lambda: None)
    assert host.beeps == [1]
    host._turboadb_error_toast.hide()
    fileutil.error_toast(host, "connect_tcp failed again", action=lambda: None)
    assert host.beeps == [1, 1]


def test_a_burst_of_updates_does_not_resize_the_toast_each_time(qapp, host):
    from PyQt5.QtCore import QEvent, QObject

    from turboadb.gui import fileutil

    toast = _show_activity(qapp, host, "warm up")

    class Counter(QObject):
        resizes = moves = 0

        def eventFilter(self, _obj, event):
            if event.type() == QEvent.Resize:
                self.resizes += 1
            elif event.type() == QEvent.Move:
                self.moves += 1
            return False

    counter = Counter()
    toast.installEventFilter(counter)
    sep = chr(92)
    try:
        for index in range(150):
            name = "IMG_20260916_%06d%s.jpg" % (index, "_burst" * (index % 4))
            fileutil.activity_toast(host, f"pull /sdcard/DCIM/Camera/{name} -> C:{sep}me{sep}{name}")
            fileutil.activity_toast(host, f"pull: {name}", level="ok")
            qapp.processEvents()
        assert counter.resizes <= 6 and counter.moves <= 6, (counter.resizes, counter.moves)
        before = (counter.resizes, counter.moves)
        for _ in range(20):  # the same message again changes nothing at all
            fileutil.activity_toast(host, "pull: done", level="ok")
        qapp.processEvents()
        assert counter.resizes <= before[0] + 1 and counter.moves <= before[1] + 1
        assert toast.message == toast.text.text() == "pull: done"
        _assert_nothing_clipped(toast.text)
    finally:
        toast.removeEventFilter(counter)


def test_a_long_saved_path_wraps_and_stays_valid_markup(qapp, host, themed):
    from PyQt5.QtCore import QRect, Qt
    from PyQt5.QtWidgets import QLabel

    from turboadb.gui import fileutil

    themed("dark")
    sep = chr(92)
    name = ("turboadb_INSTALL_FAILED_VERSION_DOWNGRADE_com.example.application_20260916_101010_"
            "screenshot_with_a_long_name_and_more_more_more.png")
    path = sep.join(["C:", "Users", "a&b <team>", "Downloads", name])
    fileutil.saved_toast(host, path, "screenshot")
    qapp.processEvents()
    note = host._turboadb_toasts[-1]
    try:
        body = next(label for label in note.findChildren(QLabel)
                    if label.objectName() == "notificationToastBody")
        plain = fileutil._plain_text(body.text())
        assert plain.replace(ZWSP, "") == name + "\n" + path
        flags = int(Qt.AlignLeft | Qt.TextWordWrap)
        needed = body.fontMetrics().boundingRect(QRect(0, 0, body.width(), _BIG), flags, plain)
        assert needed.width() <= body.width()
        assert body.heightForWidth(body.width()) <= body.height()
        assert _screen_work(qapp).contains(note.geometry())
    finally:
        note.close()
        qapp.processEvents()


def test_a_stack_trace_keeps_the_error_toast_and_its_buttons_on_screen(qapp, host, themed):
    from PyQt5.QtCore import QPoint
    from PyQt5.QtWidgets import QApplication, QPushButton

    from turboadb.gui import fileutil

    themed("dark")
    dump = "\n".join(f"  at com.android.server.pm.PackageManagerService.installStage(PMS.java:{i})"
                     for i in range(60))
    message = "adb: failed to install app.apk: Failure [INSTALL_FAILED]\n" + dump
    error = fileutil.error_toast(host, message, action=lambda: None)
    qapp.processEvents()
    work = _screen_work(qapp)
    assert work.contains(error.geometry())
    bottom = error.action.mapToGlobal(QPoint(0, error.action.height())).y()
    assert bottom <= work.bottom() + 1
    assert "shortened" in error.text.text() and error.message == message
    _assert_nothing_clipped(error.text)
    copy = next(b for b in error.findChildren(QPushButton) if b.text() == "Copy")
    copy.click()
    assert QApplication.clipboard().text() == message
    toast = _show_activity(qapp, host, "logcat: " + dump, level="warning",
                           action_text="Show log", action=lambda: None)
    assert work.contains(toast.geometry())
    assert "shortened" in toast.text.text() and toast.message == "logcat: " + dump
    _assert_nothing_clipped(toast.text)


# --------------------------------------------------------------------------- #
# the toast is never sized to something the window system refuses
# --------------------------------------------------------------------------- #
def _resize_spy(toast):
    """Record every size the toast is actually resized to."""
    from PyQt5.QtCore import QEvent, QObject

    sizes = []

    class _Spy(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Resize:
                sizes.append(event.size())
            return False

    spy = _Spy(toast)
    toast.installEventFilter(spy)
    return sizes


def test_a_longer_message_never_resizes_the_toast_to_a_height_that_cuts_it(qapp, host):
    """Activating the layout resized the shown toast to the layout minimum
    first — a height that ignores the wrapped lines. Windows refused it and Qt
    logged "QWindowsWindow::setGeometry: Unable to set geometry" every time."""
    from turboadb.gui import fileutil

    toast = fileutil.activity_toast(host, "Wi-Fi on")
    qapp.processEvents()
    sizes = _resize_spy(toast)
    long_message = ("Settings saved — terminal font size, screen renderer: screencap, "
                    "adb path, scrcpy options and audio options for this device")
    toast = fileutil.activity_toast(host, long_message)
    qapp.processEvents()
    layout = toast.layout()
    assert sizes, "the toast never resized for the longer message"
    for size in sizes:
        needed = layout.totalHeightForWidth(size.width()) if layout.hasHeightForWidth() \
            else layout.totalSizeHint().height()
        assert size.height() >= needed, (size, needed)
    assert toast.minimumSize() == toast.maximumSize() == toast.size()


def test_every_toast_size_is_one_the_toast_itself_asks_for(qapp, host, themed):
    """Whatever the message, the fixed size matches the content: no window is
    asked for a size its own layout contradicts."""
    from turboadb.gui import fileutil

    messages = ("Saved", "Copied 3 device item(s) to clipboard.",
                "make writable: " + "adb remount on every partition, " * 5,
                "Wi-Fi on")
    for message in messages:
        toast = fileutil.activity_toast(host, message)
        qapp.processEvents()
        layout = toast.layout()
        assert toast.minimumSize() == toast.maximumSize() == toast.size(), message
        assert toast.width() >= layout.totalMinimumSize().width(), message
        assert toast.height() >= layout.totalHeightForWidth(toast.width()), message


# --------------------------------------------------------------------------- #
# Settings reports what the user changed
# --------------------------------------------------------------------------- #
def test_the_settings_line_names_what_changed_not_always_the_theme():
    from turboadb.gui.main_window import _describe_settings

    assert _describe_settings({"term_font_size": 12}) == "terminal font size"
    assert _describe_settings({"screen_backend": "screencap"}) == "screen renderer: screencap"
    assert _describe_settings({"adb_path": "C:/adb.exe", "auto_update": False}) == \
        "adb path, automatic updates"
    # scrcpy's many options are named as the two groups the dialog shows
    assert _describe_settings({"scrcpy_bit_rate": "8M", "scrcpy_max_size": 1080}) == \
        "screen options"
    assert _describe_settings({"scrcpy_audio": True, "scrcpy_audio_codec": "opus"}) == \
        "audio options"
    assert _describe_settings({"scrcpy_max_size": 1080, "scrcpy_audio": True}) == \
        "screen options, audio options"
    # the theme is named only when it changed, and its bookkeeping keys never are
    assert _describe_settings({"theme": "mono-light", "theme_last_light": "mono-light",
                               "settings_version": 4}) == "theme: White"
    assert _describe_settings({}) == ""
    long_change = {"term_font": "Cascadia", "term_font_size": 12, "adb_path": "a",
                   "scrcpy_path": "s", "logcat_format": "brief"}
    assert _describe_settings(long_change) == \
        "terminal font, terminal font size, adb path and 2 more"
    # a key with no friendly name still reads as words
    assert _describe_settings({"some_new_option": 1}) == "some new option"
