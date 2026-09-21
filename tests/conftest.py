"""Shared pytest fixtures: a fake adb that records argv and returns canned
output, so the real ADBHandler code paths (arg building, parsing, safe-mode) run
without a device or a real adb binary — portable across Linux/Windows CI."""
import gc
import os
import sys
import tempfile

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys.path[0] != repo_root:
    sys.path.insert(0, repo_root)

# Never let GUI tests read or write the developer's real ~/.turboadb profile.
# Besides leaking state between runs, a redirected/profile-scanned home directory
# can make a tiny settings or terminal-log write hang the suite.
_TEST_HOME = tempfile.mkdtemp(prefix="turboadb-tests-")
os.environ["HOME"] = _TEST_HOME
os.environ["USERPROFILE"] = _TEST_HOME
# A MainWindow built by a test runs its startup tool check.  With auto-fetch on
# it downloaded platform-tools on a worker thread that outlived the test, and
# Qt later crashed the whole run (access violation).  Tests that exercise
# auto-fetch opt back in with monkeypatch.setenv.
os.environ["TURBOADB_AUTO_FETCH"] = "0"
# Qt's platform plugin is chosen once per process, by whichever test happens to
# create the QApplication first.  Several test modules set this themselves, so a
# GUI test could pass when run alone and crash natively in the full run.  conftest
# is imported before any test module, so setting it here decides it for everyone.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


@pytest.fixture(autouse=True)
def _fresh_host_lookups():
    """Clear the scrcpy host-lookup memo between tests.

    resolve_host()/is_local_host() memoise their blocking name lookups for 30 s,
    so without this a value resolved by one test would leak into the next one's
    monkeypatched socket and make the outcome depend on test order."""
    from turboadb import scrcpy

    scrcpy.resolve_host.cache_clear()
    scrcpy.is_local_host.cache_clear()
    yield
    scrcpy.resolve_host.cache_clear()
    scrcpy.is_local_host.cache_clear()


@pytest.fixture(autouse=True)
def _no_real_device_connect(monkeypatch):
    """GUI tests build DeviceTabs for made-up serials such as "123". Their
    automatic connect ran the real ``adb -s 123 wait-for-device``, which waits
    for a device that never appears; when pytest exited, that adb child was
    orphaned, and dozens piled up across runs. The connect thread still starts
    (tab lifecycle stays as in the app) but runs no adb; tests that need a
    connection call ``_on_connected`` with a fake handler."""
    try:
        from turboadb.gui import device_tab
    except Exception:  # PyQt5 is not installed (CI installs only [test])
        yield
        return
    monkeypatch.setattr(device_tab._ConnectThread, "run", lambda self: None)
    yield


@pytest.fixture(autouse=True)
def _no_ffmpeg_download(monkeypatch):
    """A webcam panel scans for cameras when it opens, and without ffmpeg that
    started the real ~160 MB download. The worker outlived its test and crashed
    a later one when its signals reached deleted widgets (and slowed the suite
    threefold). The download test re-enables it with a fake network."""
    try:
        from turboadb.gui import ffmpeg_tools
    except Exception:
        yield
        return
    monkeypatch.setattr(ffmpeg_tools, "_auto_download_supported", lambda: False)
    yield


@pytest.fixture(autouse=True)
def _settle_qt():
    """Let Qt finish with a test's widgets before the next test starts.

    Tests call ``close()`` and ``deleteLater()``, but with no event loop running
    those deletions only queue up: they are carried out by whichever later test
    happens to spin the event loop. By then Python may have dropped the last
    reference and freed the C++ object with it, so the queued delete lands on
    freed memory — a native access violation in the middle of an unrelated test
    (seen in the console tests, and only in a full run). Draining the queue here
    keeps every test's cleanup inside that test."""
    yield
    try:
        from PyQt5.QtCore import QEvent
        from PyQt5.QtWidgets import QApplication
    except Exception:  # PyQt5 is not installed (CI installs only [test])
        return
    app = QApplication.instance()
    if app is None:
        return
    # Collect first: a widget the test dropped is deleted here, and Qt drops
    # its posted events with it. Collected *during* the dispatch below, the
    # same deletion leaves Qt holding a pointer to freed memory.
    gc.collect()
    try:
        app.processEvents()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
    except RuntimeError:
        pass


@pytest.fixture(scope="session")
def qapp():
    """The ONE QApplication for the whole pytest process.

    Qt allows a single QApplication per process and fixes the platform plugin
    when it is created, so every GUI test module must share this fixture rather
    than build its own — competing fixtures made the outcome depend on test
    order.  Also exposed as ``app`` for modules that used that name."""
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(["turboadb-test"])
    return app


@pytest.fixture(scope="session")
def app(qapp):
    """Alias of :func:`qapp` (some modules ask for ``app``)."""
    return qapp
