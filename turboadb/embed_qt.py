"""TurboADB as a widget of your own Qt program: PyQt5, PySide6, PyQt6 or PySide2.

    from turboadb.embed_qt import TurboADBWidget

    view = TurboADBWidget(hide=("menubar",))       # put it in any layout
    view.started.connect(lambda: view.client.open("R58M12345", page="files"))
    view.event_received.connect(lambda e: print(e["type"], e))
    layout.addWidget(view)

TurboADB runs as a process of its own inside the widget, so its Qt never
meets your program's: a PySide6 program holds it as well as a PyQt5 one.
Some of its pages need more room than others; :attr:`minimum_changed` says
how much, and ``follow_minimum=True`` makes it the widget's own minimum size,
so your layout makes room instead of cutting TurboADB off.
:attr:`TurboADBWidget.client` is the :class:`turboadb.embed.Embedded` client
for everything else the API offers.  The widget uses the Qt your program has
already imported (else the first of PyQt5, PySide6, PyQt6, PySide2 found).
"""

from __future__ import annotations

import importlib
import math
import sys
import threading

from .embed import Embedded, TurboADBError, launch

_BINDINGS = ("PyQt5", "PySide6", "PyQt6", "PySide2")


def _binding() -> str:
    for name in _BINDINGS:
        if name in sys.modules:
            return name
    for name in _BINDINGS:
        try:
            importlib.import_module(f"{name}.QtWidgets")
            return name
        except ImportError:
            continue
    raise ImportError("turboadb.embed_qt needs PyQt5, PySide6, PyQt6 or PySide2")


BINDING = _binding()
QtCore = importlib.import_module(f"{BINDING}.QtCore")
QtWidgets = importlib.import_module(f"{BINDING}.QtWidgets")
Signal = getattr(QtCore, "pyqtSignal", None) or QtCore.Signal


def _attribute(name: str):
    """A Qt.WidgetAttribute in any binding (PyQt6 has no short names)."""
    found = getattr(QtCore.Qt, name, None)
    return found if found is not None else getattr(QtCore.Qt.WidgetAttribute, name)


class TurboADBWidget(QtWidgets.QWidget):
    """A widget that holds a whole TurboADB (see the module's notes).

    It starts TurboADB the first time it is shown (or on :meth:`start`), and
    TurboADB closes with it.  Signals, all on the widget's thread:
    :attr:`started` (``client`` can be used), :attr:`failed` (why not),
    :attr:`event_received` (each event of the API), :attr:`finished` (it has
    closed), :attr:`minimum_changed` (width, height: the smallest this widget
    can be, in its own logical pixels, without cutting TurboADB off; also
    :attr:`turboadb_minimum`).  With ``follow_minimum=True`` the widget takes
    that as its minimum size (never below 320 x 240); without, the widget's
    minimum stays yours to set.  A new minimum counts once it has held for
    :attr:`SETTLE_MS` (TurboADB's minimum jumps for a moment as it starts,
    and a layout that followed each jump would jump with it).  (Not
    ``event``: that name is QWidget's own, and taking it crashed the
    widget.)"""

    started = Signal()
    failed = Signal(str)
    event_received = Signal(object)
    finished = Signal()
    minimum_changed = Signal(int, int)

    BASE_MINIMUM = (320, 240)
    SETTLE_MS = 300

    def __init__(self, parent=None, *, exe=None, hide=(), autostart: bool = True,
                 timeout: float = 90.0, follow_minimum: bool = False):
        super().__init__(parent)
        # A window of its own (a handle) for TurboADB to go into, without
        # turning the widgets around it into windows as well.
        self.setAttribute(_attribute("WA_NativeWindow"), True)
        self.setAttribute(_attribute("WA_DontCreateNativeAncestors"), True)
        self.setMinimumSize(*self.BASE_MINIMUM)
        self.client: Embedded | None = None
        self._exe = exe
        self._hide = tuple(hide)
        self._autostart = autostart
        self._timeout = timeout
        self._follow_minimum = bool(follow_minimum)
        self._reported = None  # TurboADB's last minimum report (GET /v1/ui, min_size)
        self.turboadb_minimum = None  # (width, height) in this widget's pixels, once known
        self._settle = QtCore.QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(self.SETTLE_MS)
        self._settle.timeout.connect(self._apply_minimum)
        self._starting = False
        self._closed = False
        # results of the worker threads, handed to this widget's thread
        self._result.connect(self._on_result)
        # The program quits: TurboADB closes now, as File > Exit does, rather
        # than when it notices its window gone.  (Only hidden, as a page of a
        # tab widget is, it stays: the window is still there.)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_quit)

    _result = Signal(object)

    def start(self) -> None:
        """Start TurboADB inside this widget (once)."""
        if self._starting or self.client is not None or self._closed:
            return
        self._starting = True
        parent = int(self.winId())

        def work():
            try:
                self._result.emit(("started", launch(parent, exe=self._exe, hide=self._hide,
                                                     timeout=self._timeout)))
            except TurboADBError as exc:
                self._result.emit(("failed", str(exc)))

        threading.Thread(target=work, name="turboadb-embed-start", daemon=True).start()

    def _on_result(self, item) -> None:
        kind, value = item
        if kind == "started":
            self._starting = False
            if self._closed:
                threading.Thread(target=value.close, daemon=True).start()
                return
            self.client = value
            threading.Thread(target=self._follow_events, args=(value,),
                             name="turboadb-embed-events", daemon=True).start()
            self.started.emit()
        elif kind == "failed":
            self._starting = False
            self.failed.emit(value)
        elif kind == "event":
            if isinstance(value, dict) and value.get("type") == "min_size":
                self._take_minimum(value)
            self.event_received.emit(value)
        elif kind == "minimum":
            self._take_minimum(value)
        elif kind == "finished":
            self.client = None
            self.finished.emit()

    def _follow_events(self, client: Embedded) -> None:
        try:
            # Its minimum as it is now, then every change (min_size events)
            # from the event that was the newest before it was read.  An older
            # TurboADB has no minimum to tell: then nothing is learnt.
            try:
                after = client.info()["events"]
                minimum = client.ui()
            except Exception:  # (only a size hint: never a reason to stop)
                after, minimum = None, None
            if minimum is not None:
                self._result.emit(("minimum", minimum))
            for found in client.events() if after is None else client.events(after):
                self._result.emit(("event", found))
            self._result.emit(("finished", None))
        except RuntimeError:
            pass  # the widget has gone (and TurboADB with it)

    # ---- TurboADB's minimum size ----
    def _take_minimum(self, report) -> None:
        if not isinstance(report, dict) or "min_width_px" not in report:
            return
        self._reported = report
        self._settle.start()  # (again: only a minimum that holds counts)

    def _apply_minimum(self) -> None:
        """TurboADB's minimum in device pixels, as this widget's logical
        pixels (the two programs may scale differently)."""
        report = self._reported
        if report is None:
            return
        try:
            ratio = float(self.devicePixelRatioF() or 1.0)
            width = math.ceil(round(int(report["min_width_px"]) / ratio, 6))
            height = math.ceil(round(int(report["min_height_px"]) / ratio, 6))
        except (KeyError, TypeError, ValueError):
            return
        if (width, height) != self.turboadb_minimum:
            self.turboadb_minimum = (width, height)
            if self._follow_minimum:
                self.setMinimumSize(max(self.BASE_MINIMUM[0], width),
                                    max(self.BASE_MINIMUM[1], height))
            self.minimum_changed.emit(width, height)

    def stop(self) -> None:
        """Close TurboADB (it also closes by itself when this widget goes)."""
        self._closed = True
        client = self.client
        if client is not None:
            threading.Thread(target=client.close, daemon=True).start()

    def _on_quit(self) -> None:
        self._closed = True
        client = self.client
        if client is not None:
            try:
                client.quit()
            except TurboADBError:
                pass

    def showEvent(self, event):
        super().showEvent(event)
        if self._autostart:
            self.start()

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)
