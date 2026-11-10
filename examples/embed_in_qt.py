"""A program of your own with TurboADB inside it (PyQt5, PySide6, PyQt6 or PySide2).

    python examples/embed_in_qt.py            # with PyQt5
    python examples/embed_in_qt.py PySide6    # with PySide6 (TurboADB itself still runs PyQt5)

The toolbar is this program's own: it lists the devices TurboADB sees, opens
one on a page, and turns TurboADB's sidebar on and off.  Everything below the
toolbar is TurboADB, running as a process of its own inside this window.
Needs TurboADB with its GUI in this Python:  pip install "turboadb[gui]".
"""

import importlib
import sys

if len(sys.argv) > 1:
    importlib.import_module(f"{sys.argv[1]}.QtWidgets")  # the Qt to use, before embed_qt picks

from turboadb.embed import TurboADBError  # noqa: E402
from turboadb.embed_qt import BINDING, QtWidgets, TurboADBWidget  # noqa: E402

PAGES = ("terminal", "logcat", "files", "controls", "apps", "phone")


class HostWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"My program ({BINDING}) with TurboADB inside")
        bar = self.addToolBar("Devices")
        self.devices = QtWidgets.QComboBox()
        self.devices.setMinimumWidth(240)
        self.pages = QtWidgets.QComboBox()
        self.pages.addItems(PAGES)
        open_button = QtWidgets.QPushButton("Open")
        open_button.clicked.connect(self.open_device)
        sidebar = QtWidgets.QCheckBox("TurboADB's sidebar")
        sidebar.setChecked(True)
        sidebar.toggled.connect(lambda on: self.ask(lambda c: c.set_ui(sidebar=on)))
        for widget in (QtWidgets.QLabel(" Device: "), self.devices, QtWidgets.QLabel(" Page: "),
                       self.pages, open_button, sidebar):
            bar.addWidget(widget)

        self.turboadb = TurboADBWidget(hide=("menubar",))
        self.turboadb.started.connect(self.refresh_devices)
        self.turboadb.failed.connect(lambda why: self.statusBar().showMessage(f"TurboADB: {why}"))
        self.turboadb.event_received.connect(self.on_event)
        self.setCentralWidget(self.turboadb)
        self.statusBar().showMessage("Starting TurboADB…")
        self.resize(1400, 900)

    def ask(self, request):
        """One request to TurboADB, its error on the status bar."""
        client = self.turboadb.client
        if client is None:
            self.statusBar().showMessage("TurboADB is not running yet")
            return None
        try:
            return request(client)
        except TurboADBError as exc:
            self.statusBar().showMessage(str(exc))
            return None

    def refresh_devices(self):
        found = self.ask(lambda c: c.devices()) or []
        current = self.devices.currentText()
        self.devices.clear()
        self.devices.addItems([d["serial"] for d in found if d["online"]])
        if current:
            self.devices.setCurrentText(current)
        self.statusBar().showMessage(f"TurboADB is running · {len(found)} device(s)")

    def open_device(self):
        serial = self.devices.currentText()
        if serial:
            self.ask(lambda c: c.open(serial, page=self.pages.currentText()))

    def on_event(self, event):
        if event["type"] == "device":
            self.refresh_devices()
        elif event["type"] == "page":
            self.statusBar().showMessage(f"{event['serial']}: {event['page']}")


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = HostWindow()
    window.show()
    sys.exit(app.exec() if hasattr(app, "exec") else app.exec_())
