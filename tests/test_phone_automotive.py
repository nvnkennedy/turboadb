"""Phone tab on Android Automotive head units: calls come from a phone connected
over Bluetooth, and the driver is a secondary user.

The engine tests run everywhere (fake adb, text fixtures). The page tests need
PyQt5 and use a fake handler, so no adb, device or network is involved.
"""

from __future__ import annotations

import sys
import time

import pytest

from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBCommandError, ADBError
from turboadb.results import OperationResult

# --------------------------------------------------------------------------- #
# Fixtures: device output
# --------------------------------------------------------------------------- #
HFP_ACCOUNT = (
    "[[{mark}] PhoneAccount: ComponentInfo{{com.android.bluetooth/"
    "com.android.bluetooth.hfpclient.HfpClientConnectionService}}, ***, UserHandle{{10}} "
    "Capabilities: CallProvider Audio Routes: BEHSW Schemes: tel  Extras: Bundle[{{}}] "
    "GroupId: *** SC Restrictions: [] ]"
)
SIM_ACCOUNT = (
    "[[X] PhoneAccount: ComponentInfo{com.android.phone/"
    "com.android.services.telephony.TelephonyConnectionService}, ***, UserHandle{0} "
    "Capabilities: CallProvider MultiUser PlaceEmerg SimSub  Audio Routes: BEHSW "
    "Schemes: voicemail tel  Extras: Bundle[mParcelledData.dataSize=356] GroupId: *** "
    "SC Restrictions: [] ]"
)


def _telecom(*accounts):
    rows = "".join(f"    {account}\n" for account in accounts)
    return (
        "CallsManager: \n"
        "  mCalls: \n"
        "  mCallAudioManager:\n"
        "    All calls:\n"
        "PhoneAccountRegistrar: \n"
        "  xmlVersion: 9\n"
        "  defaultOutgoing: none\n"
        "  simCallManager: null\n"
        "  phoneAccounts:\n"
        f"{rows}"
        "  test emergency PhoneAccountHandle filter: null\n"
        "Analytics:\n"
        "  Historical Calls:\n"
    )


TELECOM_PHONE_CONNECTED = _telecom(HFP_ACCOUNT.format(mark="X"))
TELECOM_NO_PHONE = _telecom()
TELECOM_HFP_DISABLED = _telecom(HFP_ACCOUNT.format(mark=" "))
TELECOM_SIM = _telecom(SIM_ACCOUNT)
NO_TELECOM = "Can't find service: telecom"

_BT_HEAD = """Bluetooth Status
  enabled: true
  state: ON
  address: 22:22:9A:B6:17:30
  name: Car
  time since enabled: 01:02:03.456

Enable log:
  09-16 08:00:01  Enabled by android

Bluetooth crashed 0 times

AdapterProperties
  Name: Car
  Address: 22:22:9A:B6:17:30
  ScanMode: SCAN_MODE_CONNECTABLE
  ConnectionState: STATE_CONNECTED
  State: STATE_ON
  Bonded devices:
    AA:BB:CC:11:22:33 [ DUAL ] Pixel 7
"""

BT_HFP_CONNECTED = _BT_HEAD + """
Profile: HeadsetClientService
  ==== StateMachine for AA:BB:CC:11:22:33 ====
    mCurrentDevice: AA:BB:CC:11:22:33(Pixel 7) name=HeadsetClientStateMachine state=Connected
    mAudioState: 0
    mAudioWbs: false
    mCalls:
  StateMachineLog:
    HeadsetClientStateMachine:
     total records=2
     rec[0]: time=09-16 08:01:10.100 processed=Disconnected org=Disconnected dest=Connecting what=1(0x1)
     rec[1]: time=09-16 08:01:10.900 processed=Connecting org=Connecting dest=Connected what=100(0x64)
    curState=Connected

Profile: A2dpSinkService
  mCurrentDevice: AA:BB:CC:11:22:33 name=A2dpSinkStateMachine state=Connected

Profile: PbapClientService
"""

# The phone still streams music (A2DP sink) but its HFP link is down: no calls.
BT_HFP_DISCONNECTED = _BT_HEAD + """
Profile: HeadsetClientService
  ==== StateMachine for AA:BB:CC:11:22:33 ====
    mCurrentDevice: AA:BB:CC:11:22:33(Pixel 7) name=HeadsetClientStateMachine state=Disconnected
    mAudioState: 0
  StateMachineLog:
    HeadsetClientStateMachine:
     total records=1
     rec[0]: time=09-16 08:01:10.100 processed=Connected org=Connected dest=Disconnected what=2(0x2)
    curState=Disconnected

Profile: A2dpSinkService
  mCurrentDevice: AA:BB:CC:11:22:33 name=A2dpSinkStateMachine state=Connected
"""

BT_HFP_IDLE = _BT_HEAD + """
Profile: HeadsetClientService

Profile: A2dpSinkService
"""

BT_OFF = """Bluetooth Status
  enabled: false
  state: OFF
  address: 22:22:9A:B6:17:30
  name: Car
  time since disabled: 00:00:12.345

Enable log:
  09-16 08:00:01  Disabled by android
"""

# A phone's own dump: the audio-gateway side (HeadsetService), not an HFP client.
BT_PHONE_AG = _BT_HEAD + """
Profile: HeadsetService
  mActiveDevice: AA:BB:CC:44:55:66
  ==== StateMachine for AA:BB:CC:44:55:66 ====
    mCurrentDevice: AA:BB:CC:44:55:66 name=HeadsetStateMachine state=Connected
"""

CAR_SUPPORT = """@@features
feature:android.hardware.bluetooth
feature:android.hardware.type.automotive
@@dial
priority=0 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=true
com.android.car.dialer/.ui.TelecomActivity
@@call
priority=0 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=true
com.android.server.telecom/.components.UserCallActivity
@@sms
No activity found
@@apps
1 activities found:
  Activity #0:
    priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true
    com.android.car.dialer/.ui.TelecomActivity
@@kind
__turboadb_ch__
automotive
__turboadb_wm__
Physical size: 1920x1080
"""

PHONE_SUPPORT = """@@features
feature:android.hardware.bluetooth
feature:android.hardware.telephony
@@dial
priority=0 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=true
com.android.contacts/com.android.dialer.TwelveKeyDialer
@@call
priority=1 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=true
com.android.server.telecom/.components.UserCallActivity
@@sms
priority=0 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=true
com.google.android.apps.messaging/.ui.conversation.LaunchConversationActivity
@@apps
1 activities found:
  Activity #0:
    priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true
    com.android.contacts/com.android.dialer.TwelveKeyDialer
@@kind
__turboadb_ch__
nosdcard
__turboadb_wm__
Physical size: 1080x2400
"""

CALL_ROWS = "Row: 0 type=1, date=1726470000000, duration=65, number=+15550100\n"
SMS_ROWS = "Row: 0 type=1, date=1726470000000, address=+15550100, body=hi\n"


def _shell_calls(fake_adb, word):
    return [argv for argv in fake_adb.calls if word in " ".join(argv)]


def _query_words(argv):
    """The words after ``content query`` in one recorded adb argv."""
    return argv[argv.index("query") + 1:]


# --------------------------------------------------------------------------- #
# Foreground user: am get-current-user and content query --user
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, user",
    [
        ("10", 10),
        ("10\r\n", 10),
        ("0", 0),
        ("", None),
        ("Unknown command: get-current-user", None),
        ("Error: java.lang.SecurityException: Shell does not have permission", None),
        ("Activity manager (activity) commands:\n  get-current-user\n  --user <USER_ID> 0", None),
    ],
)
def test_current_user_output_is_parsed(text, user):
    assert ADBHandler.parse_current_user(text) == user


def test_car_call_log_is_read_as_the_driver(fake_adb):
    """AAOS: user 0 is headless, the driver is user 10."""
    fake_adb.add("get-current-user", stdout="10\n")
    fake_adb.add("content query", stdout=CALL_ROWS)
    rows = ADBHandler(ADBConfig(serial="ivi")).call_log(20)
    assert rows == [
        {"type": "1", "date": "1726470000000", "duration": "65", "number": "+15550100"}
    ]
    words = _query_words(fake_adb.argv_after_adb())
    assert words[:4] == ["--uri", "content://call_log/calls", "--user", "10"]


def test_car_messages_are_read_as_the_driver(fake_adb):
    fake_adb.add("get-current-user", stdout="10\n")
    fake_adb.add("content query", stdout=SMS_ROWS)
    rows = ADBHandler(ADBConfig(serial="ivi")).sms_list(5)
    assert rows[0]["body"] == "hi"
    assert _query_words(fake_adb.argv_after_adb())[:4] == ["--uri", "content://sms", "--user", "10"]


@pytest.mark.parametrize(
    "answer",
    [
        {"stdout": "0\n"},  # a normal phone
        {"stdout": "", "stderr": "Unknown command: get-current-user", "returncode": 255},
        {"stdout": "Unknown command: get-current-user\n"},  # an old Android exits 0
    ],
)
def test_phone_query_is_unchanged(fake_adb, answer):
    fake_adb.add("get-current-user", **answer)
    fake_adb.add("content query", stdout=CALL_ROWS)
    ADBHandler(ADBConfig(serial="x")).call_log(20)
    assert "--user" not in fake_adb.argv_after_adb()
    # exactly the command a phone always received
    assert fake_adb.argv_after_adb() == [
        "-s", "x", "shell", "content", "query", "--uri", "content://call_log/calls",
        "--projection", "type:date:duration:number", "--sort", "'date DESC'",
    ]


def test_current_user_is_asked_once_for_calls_and_messages(fake_adb):
    fake_adb.add("get-current-user", stdout="10\n")
    fake_adb.add("content query", stdout=CALL_ROWS)
    dev = ADBHandler(ADBConfig(serial="ivi"))
    dev.call_log(5)
    dev.sms_list(5)
    assert len(_shell_calls(fake_adb, "get-current-user")) == 1
    queries = _shell_calls(fake_adb, "content query")
    assert len(queries) == 2 and all("--user" in argv for argv in queries)


def test_current_user_is_asked_again_after_a_failed_query(fake_adb):
    """The driver may have switched (and a guest user been removed)."""
    fake_adb.add("get-current-user", stdout="11\n")
    fake_adb.add(
        "content query",
        stderr="Error while accessing provider:call_log\n"
        "java.lang.IllegalArgumentException: Invalid user 11",
    )
    dev = ADBHandler(ADBConfig(serial="ivi"))
    with pytest.raises(ADBCommandError):
        dev.call_log(5)
    with pytest.raises(ADBCommandError):
        dev.call_log(5)
    assert len(_shell_calls(fake_adb, "get-current-user")) == 2


def test_current_user_is_asked_again_after_a_while(fake_adb, monkeypatch):
    fake_adb.add("get-current-user", stdout="10\n")
    dev = ADBHandler(ADBConfig(serial="ivi"))
    dev.call_log(5)
    now = time.monotonic()
    monkeypatch.setattr(
        "turboadb.core.time.monotonic", lambda: now + ADBHandler._FOREGROUND_USER_TTL + 1
    )
    dev.call_log(5)
    assert len(_shell_calls(fake_adb, "get-current-user")) == 2


PLAIN_CALL_QUERY = [
    "-s", "x", "shell", "content", "query", "--uri", "content://call_log/calls",
    "--projection", "type:date:duration:number", "--sort", "'date DESC'",
]


@pytest.fixture
def slow_current_user(fake_adb, monkeypatch):
    """``am get-current-user`` times out (a slow or busy device) while every
    other adb command answers through ``fake_adb``. ``state["hang"] = False``
    lets it answer "10" again. Timeouts asked for are kept in ``state``."""
    import subprocess

    state = {"hang": True, "timeouts": []}
    fake_adb.add("get-current-user", stdout="10\n")
    fake_adb.add("content query", stdout=CALL_ROWS)

    def run(cmd, **kw):
        if "get-current-user" in " ".join(cmd):
            state["timeouts"].append(kw.get("timeout"))
            if state["hang"]:
                fake_adb.calls.append(list(cmd))
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return fake_adb.run(cmd, **kw)

    monkeypatch.setattr("turboadb.core.subprocess.run", run)
    return state


def test_current_user_timeout_still_reads_the_call_log(fake_adb, slow_current_user):
    """Regression: a timed-out lookup raised and the phone lost its call log."""
    rows = ADBHandler(ADBConfig(serial="x")).call_log(20)
    assert rows and rows[0]["number"] == "+15550100"
    assert fake_adb.argv_after_adb() == PLAIN_CALL_QUERY  # no --user, query still ran
    # only a hint: a short wait, not the 15 s a real query may take
    assert slow_current_user["timeouts"] and max(slow_current_user["timeouts"]) <= 5
    res = ADBHandler(ADBConfig(serial="x"), safe=True).call_log(20)
    assert res.success and res.value == rows


def test_current_user_timeout_is_cached(fake_adb, slow_current_user):
    dev = ADBHandler(ADBConfig(serial="x"))
    dev.call_log(5)
    dev.sms_list(5)
    dev.call_log(5)
    assert len(_shell_calls(fake_adb, "get-current-user")) == 1  # waited once, not per load
    queries = _shell_calls(fake_adb, "content query")
    assert len(queries) == 3 and not any("--user" in argv for argv in queries)


def test_current_user_is_used_once_the_cached_failure_expires(
    fake_adb, slow_current_user, monkeypatch
):
    dev = ADBHandler(ADBConfig(serial="ivi"))
    dev.call_log(5)
    assert "--user" not in fake_adb.argv_after_adb()

    slow_current_user["hang"] = False  # the device answers again
    dev.call_log(5)
    assert "--user" not in fake_adb.argv_after_adb()  # still within the cached window
    now = time.monotonic()
    monkeypatch.setattr(
        "turboadb.core.time.monotonic", lambda: now + ADBHandler._FOREGROUND_USER_TTL + 1
    )
    dev.call_log(5)
    assert _query_words(fake_adb.argv_after_adb())[:4] == [
        "--uri", "content://call_log/calls", "--user", "10",
    ]
    assert len(_shell_calls(fake_adb, "get-current-user")) == 2


def test_car_security_exception_still_raises_in_raw_mode(fake_adb):
    fake_adb.add("get-current-user", stdout="10\n")
    fake_adb.add(
        "content query",
        stderr="java.lang.SecurityException: Permission Denial: opening provider "
        "com.android.providers.contacts.CallLogProvider requires android.permission.READ_CALL_LOG",
    )
    with pytest.raises(ADBCommandError):
        ADBHandler(ADBConfig(serial="ivi")).call_log()
    res = ADBHandler(ADBConfig(serial="ivi"), safe=True).call_log()
    assert isinstance(res, OperationResult) and not res.success


# --------------------------------------------------------------------------- #
# Is a phone connected to the car?
# --------------------------------------------------------------------------- #
def test_enabled_hfp_account_means_a_phone_is_connected():
    assert ADBHandler.parse_phone_connection(TELECOM_PHONE_CONNECTED) is True
    # the Bluetooth dump isn't needed, and can't overrule it
    assert ADBHandler.parse_phone_connection(TELECOM_PHONE_CONNECTED, BT_OFF) is True


def test_no_hfp_account_asks_bluetooth():
    parse = ADBHandler.parse_phone_connection
    assert parse(TELECOM_NO_PHONE) is None  # Telecom alone can't say "no"
    assert parse(TELECOM_NO_PHONE, BT_HFP_CONNECTED) is True
    assert parse(TELECOM_NO_PHONE, BT_HFP_DISCONNECTED) is False  # A2DP alone places no calls
    assert parse(TELECOM_NO_PHONE, BT_HFP_IDLE) is False
    assert parse(NO_TELECOM, BT_HFP_CONNECTED) is True
    assert parse(NO_TELECOM, BT_HFP_IDLE) is False


def test_disabled_hfp_account_means_no_phone():
    assert ADBHandler.parse_phone_connection(TELECOM_HFP_DISABLED) is False


def test_bluetooth_off_means_no_phone_unless_there_is_a_sim():
    parse = ADBHandler.parse_phone_connection
    # Android Bluetooth off: the unit may pair phones through its own module
    assert parse(TELECOM_NO_PHONE, BT_OFF) is None
    assert parse(TELECOM_SIM, BT_OFF) is None  # it calls through its SIM
    assert parse(NO_TELECOM, BT_OFF) is None  # Telecom unreadable: can't tell


def test_connection_is_unknown_when_nothing_says():
    parse = ADBHandler.parse_phone_connection
    assert parse("", "") is None
    assert parse(NO_TELECOM, "Can't find service: bluetooth_manager") is None
    # a phone's audio-gateway profile is not an HFP client connection
    assert parse(TELECOM_SIM, BT_PHONE_AG) is None
    assert parse(TELECOM_NO_PHONE, _BT_HEAD) is None  # no HFP client profile at all


def test_windows_line_endings_are_fine():
    crlf = BT_HFP_CONNECTED.replace("\n", "\r\n")
    assert ADBHandler.parse_phone_connection(NO_TELECOM, crlf) is True
    assert ADBHandler.parse_phone_connection(NO_TELECOM, BT_HFP_IDLE.replace("\n", "\r\n")) is False


def test_phone_connected_reads_bluetooth_only_when_telecom_cannot_tell(fake_adb):
    fake_adb.add("dumpsys telecom", stdout=TELECOM_PHONE_CONNECTED)
    assert ADBHandler(ADBConfig(serial="ivi")).phone_connected() is True
    assert _shell_calls(fake_adb, "bluetooth_manager") == []


def test_phone_connected_falls_back_to_bluetooth(fake_adb):
    fake_adb.add("dumpsys telecom", stdout=TELECOM_NO_PHONE)
    fake_adb.add("dumpsys bluetooth_manager", stdout=BT_HFP_IDLE)
    assert ADBHandler(ADBConfig(serial="ivi")).phone_connected() is False
    assert len(_shell_calls(fake_adb, "bluetooth_manager")) == 1
    res = ADBHandler(ADBConfig(serial="ivi")).phone_connected(safe=True)
    assert res.success and res.value is False


def test_phone_support_reports_the_car_and_its_phone(fake_adb):
    fake_adb.add("resolve-activity", stdout=CAR_SUPPORT)
    fake_adb.add("dumpsys telecom", stdout=TELECOM_NO_PHONE)
    fake_adb.add("dumpsys bluetooth_manager", stdout=BT_HFP_IDLE)
    info = ADBHandler(ADBConfig(serial="ivi")).phone_support()
    # the existing keys are unchanged
    assert {k: info[k] for k in ("telephony", "dialer", "caller", "messages", "phone_apps")} == (
        ADBHandler.parse_phone_support(CAR_SUPPORT)
    )
    assert info["kind"] == "automotive" and info["automotive"] is True
    assert info["phone_connected"] is False


def test_phone_support_on_a_phone_does_not_probe_bluetooth(fake_adb):
    fake_adb.add("resolve-activity", stdout=PHONE_SUPPORT)
    info = ADBHandler(ADBConfig(serial="x")).phone_support()
    assert info["kind"] == "phone" and info["automotive"] is False
    assert info["phone_connected"] is None
    assert _shell_calls(fake_adb, "dumpsys") == []
    assert len(_shell_calls(fake_adb, "shell")) == 1  # still one query


def test_phone_support_survives_a_hung_connection_check(fake_adb, monkeypatch):
    fake_adb.add("resolve-activity", stdout=CAR_SUPPORT)

    def hung(self, *, safe=None):
        raise ADBError("adb command timed out after 20s: 'shell dumpsys telecom'")

    monkeypatch.setattr(ADBHandler, "phone_connected", hung)
    info = ADBHandler(ADBConfig(serial="ivi")).phone_support()
    assert info["automotive"] is True and info["phone_connected"] is None
    assert info["dialer"] == "com.android.car.dialer/.ui.TelecomActivity"


# --------------------------------------------------------------------------- #
# The Phone page on a car
# --------------------------------------------------------------------------- #
NOW_MS = int(time.time() * 1000)
CALLS = [
    {"type": "1", "date": str(NOW_MS - 60_000), "duration": "65", "number": "+15550100"},
    {"type": "3", "date": str(NOW_MS - 120_000), "duration": "0", "number": "+15550101"},
]
SMS = [{"type": "1", "date": str(NOW_MS - 60_000), "address": "+15550100", "body": "hi"}]
DENIAL = (
    "ADBCommandError: java.lang.SecurityException: Permission Denial: opening provider "
    "com.android.providers.contacts.CallLogProvider requires android.permission.READ_CALL_LOG"
)


def _ok(action, value):
    return OperationResult(success=True, action=action, value=value)


class FakeCar:
    """An Android Automotive head unit: no telephony, Car Dialer, a phone link
    that tests switch on and off. Records every call and its ``safe`` flag."""

    def __init__(self, *, link=False, telephony=False, automotive=True,
                 calls_error=None, sms_error=None):
        self.invoked = []
        self.link = link
        self.calls_error = calls_error
        self.sms_error = sms_error
        self.support = {
            "telephony": telephony,
            "dialer": "com.android.car.dialer/.ui.TelecomActivity",
            "caller": "com.android.server.telecom/.components.UserCallActivity",
            "messages": "com.android.car.messenger/.MessengerActivity",
            "phone_apps": ["com.android.car.dialer"],
            "kind": "automotive" if automotive else "phone",
            "automotive": automotive,
            "phone_connected": link if automotive else None,
        }

    def names(self):
        return [name for name, *_ in self.invoked]

    def _result(self, action, safe, value=None, error=None):
        if error is not None:
            if safe:
                return OperationResult(success=False, action=action, error=ADBError(error))
            raise ADBError(error)
        return _ok(action, value) if safe else value

    def phone_support(self, *, safe=None):
        self.invoked.append(("phone_support", safe))
        return self._result("phone_support", safe, dict(self.support))

    def phone_connected(self, *, safe=None):
        self.invoked.append(("phone_connected", safe))
        return self._result("phone_connected", safe, self.link)

    def call_state(self, *, safe=None):
        self.invoked.append(("call_state", safe))
        return self._result("call_state", safe, "idle")

    def call_log(self, limit=50, *, safe=None):
        self.invoked.append(("call_log", safe))
        return self._result("call_log", safe, list(CALLS), self.calls_error)

    def sms_list(self, limit=50, *, safe=None):
        self.invoked.append(("sms_list", safe))
        return self._result("sms_list", safe, list(SMS), self.sms_error)

    def dial(self, number, *, safe=None):
        self.invoked.append(("dial", safe))
        return _ok("dial", True)

    def call(self, number, *, safe=None):
        self.invoked.append(("call", safe))
        return _ok("call", True)

    def answer_call(self, *, safe=None):
        self.invoked.append(("answer_call", safe))
        return _ok("keyevent", True)

    def end_call(self, *, safe=None):
        self.invoked.append(("end_call", safe))
        return _ok("keyevent", True)

    def send_sms(self, number, body, *, safe=None):
        self.invoked.append(("send_sms", safe))
        return _ok("send_sms", True)


def _process_events(qapp):
    """processEvents() that tolerates stale callbacks of widgets from earlier tests."""
    errors = []
    previous = sys.excepthook
    sys.excepthook = lambda etype, value, tb: errors.append(value)
    try:
        qapp.processEvents()
    finally:
        sys.excepthook = previous
    for error in errors:
        if not (isinstance(error, RuntimeError) and "has been deleted" in str(error)):
            raise error


def _pump(qapp, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _process_events(qapp)
        if predicate():
            return True
        time.sleep(0.01)
    _process_events(qapp)
    return predicate()


@pytest.fixture
def car_panel(qapp):
    """Build PhonePanels on fake handlers; close them and wait for their jobs."""
    pytest.importorskip("PyQt5")
    from turboadb.gui.phone_panel import PhonePanel
    from turboadb.gui.qtutil import thread_running

    created = []

    def _make(handler):
        panel = PhonePanel(handler)
        panel.logs = []
        panel.log.connect(panel.logs.append)
        created.append(panel)
        return panel

    yield _make
    threads = []
    for panel in created:
        threads.extend(panel._jobs)
        panel.close_panel()
    for thread in threads:
        if thread_running(thread):
            thread.wait(5000)
    _process_events(qapp)
    for panel in created:
        panel.hide()
        panel.deleteLater()
    _process_events(qapp)


def _no_warnings(panel):
    return not any(m.startswith(("[WARNING]", "[ERROR]")) for m in panel.logs)


def _shows_no_phone(panel):
    return (
        panel.calls_empty.title.text() == "No phone connected"
        and panel.sms_empty.title.text() == "No phone connected"
    )


def test_car_without_a_phone_shows_a_calm_state(qapp, car_panel):
    """Issue: call history 'not working' on Android Automotive with no phone connected."""
    handler = FakeCar(link=False)
    panel = car_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: _shows_no_phone(panel))
    assert panel.calls_stack.currentWidget() is panel.calls_empty
    assert panel.sms_stack.currentWidget() is panel.sms_empty
    assert "Bluetooth" in panel.calls_empty.detail.text()
    assert panel.calls_empty.glyph.icon_name() == ("bluetooth", "dim")  # neutral, not amber
    assert panel.call_state_text() == "No phone"
    assert (panel.state_pill.property("state") or "") == ""
    assert "No phone connected" in panel.hint.text()
    assert panel.count.text() == ""
    _pump(qapp, lambda: False, timeout=0.2)
    assert _no_warnings(panel)
    # nothing is read from the device while no phone is connected
    assert not {"call_log", "sms_list", "call_state"} & set(handler.names())

    panel.number.setText("+15550100")
    panel.sms_to.setText("+15550100")
    for button in (panel.btn_call, panel.btn_dial, panel.btn_answer, panel.btn_end,
                   panel.btn_compose):
        assert not button.isEnabled()
        assert "No phone connected" in button.toolTip()
    panel.number.returnPressed.emit()  # Enter doesn't open the dialler either
    panel._answer_call()
    panel._end_call()
    panel._compose()
    _pump(qapp, lambda: False, timeout=0.2)
    assert not {"dial", "call", "answer_call", "end_call", "send_sms"} & set(handler.names())
    assert "No phone connected" in panel.dial_hint.text()


def test_car_without_a_phone_polls_lightly_and_only_while_visible(qapp, car_panel):
    handler = FakeCar(link=False)
    panel = car_panel(handler)
    panel.show()
    assert _pump(qapp, lambda: _shows_no_phone(panel))
    assert panel._link_poll.isActive()
    assert panel._link_poll.interval() >= 10000  # a modest interval
    assert not panel._poll.isActive()  # no call-state polling

    panel._link_poll.timeout.emit()  # one re-check: the connection only
    assert _pump(qapp, lambda: handler.names().count("phone_connected") == 1)
    assert _pump(qapp, lambda: not panel._link_busy)
    assert ("phone_connected", False) in handler.invoked  # raw mode: no engine error log

    panel.hide()
    assert not panel._link_poll.isActive()
    panel._check_phone_link()  # a stray poll while hidden asks nothing
    _pump(qapp, lambda: False, timeout=0.2)
    assert handler.names().count("phone_connected") == 1
    assert not {"call_log", "sms_list", "call_state"} & set(handler.names())
    assert _no_warnings(panel)


def test_connecting_a_phone_loads_its_history(qapp, car_panel):
    from turboadb.gui.phone_panel import ROW_ROLE

    handler = FakeCar(link=False)
    panel = car_panel(handler)
    panel.show()
    assert _pump(qapp, lambda: _shows_no_phone(panel))

    handler.link = True
    panel._link_poll.timeout.emit()
    assert _pump(qapp, lambda: panel.calls.count() == len(CALLS) and panel.sms.count() == len(SMS))
    assert panel.calls_stack.currentWidget() is panel.calls
    assert panel.calls.item(0).data(ROW_ROLE)["title"] == "+15550100"
    assert panel.call_state_text() == "Phone connected"
    assert any(m.startswith("[INFO] Phone connected") for m in panel.logs)
    panel.number.setText("+15550100")
    assert panel.btn_call.isEnabled() and panel.btn_answer.isEnabled() and panel.btn_end.isEnabled()
    assert "No phone" not in panel.btn_call.toolTip()
    assert ("call_log", False) in handler.invoked  # a car's expected failures stay quiet
    assert "call_state" not in handler.names()  # no telephony on this car

    handler.link = False  # and the phone drives away
    panel._link_poll.timeout.emit()
    assert _pump(qapp, lambda: _shows_no_phone(panel))
    assert panel.calls.count() == 0
    assert any(m.startswith("[INFO] Phone disconnected") for m in panel.logs)
    assert not panel.btn_answer.isEnabled()
    assert _no_warnings(panel)


def test_refresh_on_a_car_rechecks_the_phone_first(qapp, car_panel):
    handler = FakeCar(link=False)
    panel = car_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: _shows_no_phone(panel))
    handler.link = True
    panel.btn_refresh.click()
    assert _pump(qapp, lambda: panel.calls.count() == len(CALLS))
    assert handler.names().count("phone_connected") == 1


def test_car_with_a_phone_loads_calls_as_on_a_phone(qapp, car_panel):
    handler = FakeCar(link=True)
    panel = car_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: panel.calls.count() == len(CALLS) and panel.sms.count() == len(SMS))
    assert panel.count.text() == "2 calls"
    assert panel.call_state_text() == "Phone connected"
    assert _no_warnings(panel)


def test_car_permission_failure_is_a_calm_hint(qapp, car_panel):
    handler = FakeCar(link=True, calls_error=DENIAL, sms_error=DENIAL.replace("CALL_LOG", "SMS"))
    panel = car_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: panel.calls_empty.title.text() ==
                 "Call history not available on this head unit"
                 and panel.sms_empty.title.text() == "Messages not available on this head unit")
    assert panel.calls_empty.glyph.icon_name() == ("phone", "dim")
    _pump(qapp, lambda: False, timeout=0.2)
    assert _no_warnings(panel)


def test_car_that_cannot_tell_loads_as_before(qapp, car_panel):
    handler = FakeCar(link=None)
    panel = car_panel(handler)
    panel.show()
    assert _pump(qapp, lambda: panel.calls.count() == len(CALLS))
    assert not panel._link_poll.isActive()  # nothing to watch
    assert panel.btn_answer.isEnabled()
    assert "phone_connected" not in handler.names()


def test_phone_behaviour_is_unchanged(qapp, car_panel):
    handler = FakeCar(automotive=False, telephony=True, link=None)
    panel = car_panel(handler)
    panel.show()
    assert _pump(qapp, lambda: panel.calls.count() == len(CALLS) and panel.sms.count() == len(SMS))
    assert ("call_log", True) in handler.invoked and ("sms_list", True) in handler.invoked
    assert _pump(qapp, lambda: panel.call_state_text() == "Idle")
    assert panel._poll.isActive() and not panel._link_poll.isActive()
    assert "phone_connected" not in handler.names()
    panel.number.setText("+15550100")
    assert panel.btn_call.isEnabled() and panel.btn_answer.isEnabled()
    assert panel.btn_call.toolTip() == "Place a call to this number on the device"


def test_permission_denials_count_as_unsupported_on_a_car_only(qapp):
    pytest.importorskip("PyQt5")
    from turboadb.gui.phone_panel import is_denied, is_unsupported

    for message in (
        DENIAL,
        "java.lang.SecurityException: Permission Denial: reading "
        "com.android.providers.telephony.SmsProvider uri content://sms",
        "Error: requires android.permission.READ_CALL_LOG",
    ):
        assert is_denied(message)
        assert is_unsupported(message, automotive=True)
        assert not is_unsupported(message)  # on a phone a denial still warns
    assert is_unsupported("Error while accessing provider:call_log")
    assert not is_unsupported("adb: device offline", automotive=True)
