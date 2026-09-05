"""Shared pytest fixtures: a fake adb that records argv and returns canned
output, so the real ADBHandler code paths (arg building, parsing, safe-mode) run
without a device or a real adb binary — portable across Linux/Windows CI."""
import os
import sys

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys.path[0] != repo_root:
    sys.path.insert(0, repo_root)

import pytest


class FakeCompleted:
    def __init__(self, args, returncode=0, stdout=b"", stderr=b""):
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeAdb:
    """Records every adb argv and answers a configurable set of responses.

    responses: maps a matching function OR a substring (matched against the
    joined argv after the adb path) to (returncode, stdout, stderr). bytes or str
    stdout both accepted. Default: success with empty output.
    """

    def __init__(self):
        self.calls = []  # list of argv lists
        self.rules = []  # (predicate, (rc, out, err))

    def add(self, needle, stdout=b"", returncode=0, stderr=b""):
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        if callable(needle):
            pred = needle
        else:

            def pred(argv, n=needle):
                return n in " ".join(argv)

        self.rules.append((pred, (returncode, stdout, stderr)))
        return self

    def _answer(self, argv):
        for pred, resp in self.rules:
            if pred(argv):
                return resp
        return (0, b"", b"")

    def run(self, cmd, **kw):
        argv = list(cmd)
        self.calls.append(argv)
        rc, out, err = self._answer(argv)
        text = kw.get("text") or kw.get("universal_newlines")
        if text:
            out = out.decode() if isinstance(out, bytes) else out
            err = err.decode() if isinstance(err, bytes) else err
        return FakeCompleted(argv, rc, out, err)

    def last(self):
        return self.calls[-1] if self.calls else None

    def argv_after_adb(self, index=-1):
        """The call's argv with the adb executable path stripped off the front."""
        return self.calls[index][1:]


@pytest.fixture
def fake_adb(monkeypatch, tmp_path):
    """Install a FakeAdb, point find_adb at a dummy file, and patch subprocess.run
    inside turboadb.core / turboadb.tools so the handler uses it."""
    import turboadb.core as core
    import turboadb.tools as tools

    dummy = tmp_path / ("adb.exe" if __import__("os").name == "nt" else "adb")
    dummy.write_text("")
    monkeypatch.setenv("TURBOADB_ADB", str(dummy))
    monkeypatch.setenv("TURBOADB_AUTO_FETCH", "0")  # never touch the network

    fake = FakeAdb()
    monkeypatch.setattr(core.subprocess, "run", fake.run)
    monkeypatch.setattr(tools.subprocess, "run", fake.run)
    return fake


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(["turboadb-test"])
    return app
