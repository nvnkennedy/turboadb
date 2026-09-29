"""The WinRM session behind "Deploy serve" and the remote webcam.

The endpoint was built as ``http://HOST:PORT/wsman``, so an IPv6 host's colons
ran into the port's; it goes in brackets now.  The remote webcam ran its
scripts through a copy of the deploy's runner that only differed in keeping
the output unstripped: both use the one runner.
"""
import sys
import types

import pytest

from turboadb import remote_deploy
from turboadb.gui import remote_webcam as rw


@pytest.fixture
def fake_winrm(monkeypatch):
    """A pywinrm stand-in that records the endpoint of every session."""
    endpoints = []

    class Session:
        def __init__(self, endpoint, **_kwargs):
            endpoints.append(endpoint)

    monkeypatch.setitem(sys.modules, "winrm", types.SimpleNamespace(Session=Session))
    return endpoints


@pytest.mark.parametrize("host, ssl, endpoint", [
    ("10.0.0.5", False, "http://10.0.0.5:5985/wsman"),
    ("lab-pc.corp.local", True, "https://lab-pc.corp.local:5986/wsman"),
    ("fd00::5", False, "http://[fd00::5]:5985/wsman"),
    ("[fd00::5]", True, "https://[fd00::5]:5986/wsman"),
])
def test_the_endpoint_keeps_an_ipv6_host_in_brackets(fake_winrm, host, ssl, endpoint):
    remote_deploy._session(host, "DOMAIN\\user", "pw", winrm_port=5986 if ssl else 5985,
                           use_ssl=ssl)
    assert fake_winrm == [endpoint]


@pytest.fixture
def replies(monkeypatch):
    """Every WinRM session answers with padded output."""
    ran = []

    class Session:
        def run_ps(self, script):
            ran.append(script)
            return types.SimpleNamespace(status_code=0, std_out=b"  FFMPEG:C:\\ff.exe\r\n\r\n",
                                         std_err=b" warning \n")

    monkeypatch.setattr(remote_deploy, "_session", lambda *a, **k: Session())
    return ran


def test_the_deploy_strips_what_the_remote_printed(replies):
    code, out, err = remote_deploy._run_ps("pc", "u", "p", "'x'", winrm_port=5985)
    assert (code, out, err) == (0, "FFMPEG:C:\\ff.exe", "warning")


def test_the_remote_webcam_runs_through_the_same_session_unstripped(replies):
    code, out, err = rw._run_ps("pc", "u", "p", "Get-Command ffmpeg", 5985)
    assert replies == ["Get-Command ffmpeg"]
    assert (code, out, err) == (0, "  FFMPEG:C:\\ff.exe\r\n\r\n", " warning \n")
