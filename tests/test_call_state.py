"""``call_state()`` reads every SIM of the phone, and tells a device without a
call state ("unknown") from one that could not be asked (an error).

``dumpsys telephony.registry`` prints one block per SIM; only the first was
read, so a call or a ring on the second SIM showed as idle. And a failed adb
call (the phone rebooting or unplugged) came back as "unknown", which the
Phone page takes for "this device has no telephony"."""
import pytest

from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBCommandError, ADBError


def _registry(*sims):
    """A ``dumpsys telephony.registry`` dump with one block per SIM, each a
    ``(call state, ringing call state)`` pair as Android numbers them."""
    lines = ["last known state:"]
    for phone, (state, ringing) in enumerate(sims):
        lines += [
            f"Phone Id={phone}",
            f"  mCallState={state}",
            f"  mRingingCallState={ringing}",
            "  mForegroundCallState=0",
            "  mBackgroundCallState=0",
            "  mCallIncomingNumber=",
            "  mServiceState={mVoiceRegState=0(IN_SERVICE)}",
        ]
    lines += ["local logs:", "  notifyCallStateForSubscriber: subId=2 state=0"]
    return "\n".join(lines) + "\n"


_IDLE, _RINGING, _IN_CALL = (0, 0), (1, 1), (2, 0)


def _state(fake_adb, dump, **answer):
    fake_adb.add("dumpsys telephony.registry", stdout=dump, **answer)
    return ADBHandler(ADBConfig(serial="x")).call_state()


@pytest.mark.parametrize("sim, state", [
    (_IDLE, "idle"), (_RINGING, "ringing"), (_IN_CALL, "in call"),
])
def test_a_single_sim_reports_its_state(fake_adb, sim, state):
    assert _state(fake_adb, _registry(sim)) == state


@pytest.mark.parametrize("sims, state", [
    ((_IDLE, _IN_CALL), "in call"),
    ((_IDLE, _RINGING), "ringing"),
    ((_RINGING, _IDLE), "ringing"),
    ((_IN_CALL, _RINGING), "in call"),  # a second call coming in during a call
    ((_RINGING, _IN_CALL), "in call"),
    ((_IDLE, _IDLE), "idle"),
])
def test_a_call_or_a_ring_on_any_sim_counts(fake_adb, sims, state):
    assert _state(fake_adb, _registry(*sims)) == state


@pytest.mark.parametrize("ringing", [1, 2, 4])
def test_only_the_call_state_itself_is_read(fake_adb, ringing):
    # the ringing-call state of an idle SIM says nothing about a call, also
    # when its number is one that would mean a ring or a call
    assert _state(fake_adb, _registry((0, ringing))) == "idle"


@pytest.mark.parametrize("dump", [
    "",
    "Can't find service: telephony.registry\n",
    "Permission Denial: can't dump telephony.registry from pid=4242, uid=2000\n",
])
def test_a_device_that_answers_without_a_call_state_is_unknown(fake_adb, dump):
    assert _state(fake_adb, dump) == "unknown"


# --------------------------------------------------------------------------- #
# a device that could not be asked
# --------------------------------------------------------------------------- #
def test_a_failed_adb_call_is_an_error_not_unknown(fake_adb):
    # the phone is rebooting or was unplugged: nothing was learnt about its telephony
    with pytest.raises(ADBCommandError, match="device offline"):
        _state(fake_adb, "", returncode=1, stderr="adb: device offline\n")


def test_the_error_carries_what_the_device_printed(fake_adb):
    # a build whose dumpsys fails outright: the reason is on stdout there
    with pytest.raises(ADBCommandError, match="Can't find service: telephony.registry"):
        _state(fake_adb, "Can't find service: telephony.registry\n", returncode=20)


def test_safe_mode_reports_the_failed_call(fake_adb):
    fake_adb.add("dumpsys telephony.registry", returncode=1,
                 stderr="error: device 'x' not found\n")
    result = ADBHandler(ADBConfig(serial="x"), safe=True).call_state()
    assert not result.success and isinstance(result.error, ADBError)
    assert "not found" in str(result.error)
