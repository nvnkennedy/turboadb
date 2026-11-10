"""TurboADB as a widget of your own Qt program: PyQt5, PySide6, PyQt6 or PySide2.

    from turboadb.embed_qt import TurboADBWidget

    view = TurboADBWidget(hide=("menubar",))       # put it in any layout
    view.started.connect(lambda: view.client.open("R58M12345", page="files"))
    view.event_received.connect(lambda e: print(e["type"], e))
    layout.addWidget(view)

TurboADB runs as a process of its own inside the widget, so its Qt never
meets your program's: a PySide6 program holds it as well as a PyQt5 one.
:attr:`TurboADBWidget.client` is the :class:`turboadb.embed.Embedded` client
for everything else the API offers.  The widget uses the Qt your program has
already imported (else the first of PyQt5, PySide6, PyQt6, PySide2 found).
"""

from __future__ import annotations

import importlib
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
    closed).  (Not ``event``: that name is QWidget's own, and taking it
    crashed the widget.)"""

    started = Signal()
    failed = Signal(str)
    event_received = Signal(object)
    finished = Signal()

    def __init__(self, parent=None, *, exe=None, hide=(), autostart: bool = True,
                 timeout: float = 90.0):
        super().__init__(parent)
        # A window of its own (a handle) for TurboADB to go into, without
        # turning the widgets around it into windows as well.
        self.setAttribute(_attribute("WA_NativeWindow"), True)
        self.setAttribute(_attribute("WA_DontCreateNativeAncestors"), True)
        self.setMinimumSize(320, 240)
        self.client: Embedded | None = None
        self._exe = exe
        self._hide = tuple(hide)
        self._autostart = autostart
        self._timeout = timeout
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
            self.event_received.emit(value)
        elif kind == "finished":
            self.client = None
            self.finished.emit()

    def _follow_events(self, client: Embedded) -> None:
        try:
            for found in client.events():
                self._result.emit(("event", found))
            self._result.emit(("finished", None))
        except RuntimeError:
            pass  # the widget has gone (and TurboADB with it)

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
