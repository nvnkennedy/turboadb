"""Phone control: a dialler (dial / call / answer / end), the call log, and SMS
messages (read + compose) — a phone simulation driven over adb."""

from __future__ import annotations

import time

from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QLabel, QTabWidget, QTableWidget,
                             QTableWidgetItem, QHeaderView, QGroupBox)


class _Job(QThread):
    done = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


def _when(ms):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except Exception:
        return ms or ""


def _dur(sec):
    try:
        s = int(sec)
        return f"{s // 60}:{s % 60:02d}"
    except Exception:
        return sec or ""


_CALL_TYPE = {"1": "📥 in", "2": "📤 out", "3": "📵 missed", "5": "⛔ rejected"}
_SMS_TYPE = {"1": "📥 inbox", "2": "📤 sent"}


class PhonePanel(QWidget):
    log = pyqtSignal(str)

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self._jobs = []

        outer = QVBoxLayout(self)

        # ---- dialler ----
        dg = QGroupBox("Dialler")
        dl = QHBoxLayout(dg)
        self.number = QLineEdit()
        self.number.setPlaceholderText("phone number, e.g. +1 555 0100")
        self.number.returnPressed.connect(self._dial)
        b_dial = QPushButton("☎ Dial"); b_dial.setProperty("role", "ghost")
        b_dial.clicked.connect(self._dial)
        b_call = QPushButton("📞 Call"); b_call.setProperty("role", "ok")
        b_call.clicked.connect(self._call)
        b_ans = QPushButton("✅ Answer"); b_ans.setProperty("role", "ghost")
        b_ans.clicked.connect(lambda: self._do("answer", lambda h: h.answer_call(safe=False)))
        b_end = QPushButton("🔴 End"); b_end.setProperty("role", "danger")
        b_end.clicked.connect(lambda: self._do("end call", lambda h: h.end_call(safe=False)))
        self.state = QLabel("—")
        for w in (self.number, b_dial, b_call, b_ans, b_end, self.state):
            dl.addWidget(w)
        dl.setStretch(0, 1)
        outer.addWidget(dg)

        # ---- call log + messages ----
        self.tabs = QTabWidget()
        self.tabs.addTab(self._calllog_tab(), "📋 Call log")
        self.tabs.addTab(self._sms_tab(), "💬 Messages")
        outer.addWidget(self.tabs, 1)

        self._loaded = False        # load lazily on first view (keeps connect fast)

    def showEvent(self, event):
        super().showEvent(event)
        # don't query the device on connect — only when the user opens this tab,
        # so the initial connect stays fast (esp. over a remote/RDP link)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    # ---- tabs ----
    def _calllog_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        bar = QHBoxLayout()
        r = QPushButton("Refresh"); r.setProperty("role", "ghost")
        r.clicked.connect(self._load_calls)
        bar.addWidget(r); bar.addStretch(1)
        v.addLayout(bar)
        self.calls = QTableWidget(0, 4)
        self.calls.setHorizontalHeaderLabels(["Type", "Number", "When", "Duration"])
        self.calls.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.calls.setEditTriggers(QTableWidget.NoEditTriggers)
        self.calls.setAlternatingRowColors(True)
        self.calls.verticalHeader().setVisible(False)
        self.calls.cellDoubleClicked.connect(self._call_from_log)
        v.addWidget(self.calls, 1)
        return w

    def _sms_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        comp = QHBoxLayout()
        self.sms_to = QLineEdit(); self.sms_to.setPlaceholderText("to (number)")
        self.sms_to.setMaximumWidth(180)
        self.sms_body = QLineEdit(); self.sms_body.setPlaceholderText("message…")
        b_send = QPushButton("✉ Compose"); b_send.setProperty("role", "ok")
        b_send.clicked.connect(self._send_sms)
        r = QPushButton("Refresh"); r.setProperty("role", "ghost")
        r.clicked.connect(self._load_sms)
        for x in (self.sms_to, self.sms_body, b_send, r):
            comp.addWidget(x)
        comp.setStretch(1, 1)
        v.addLayout(comp)
        self.sms = QTableWidget(0, 4)
        self.sms.setHorizontalHeaderLabels(["Type", "Address", "When", "Message"])
        self.sms.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.sms.setEditTriggers(QTableWidget.NoEditTriggers)
        self.sms.setAlternatingRowColors(True)
        self.sms.verticalHeader().setVisible(False)
        v.addWidget(self.sms, 1)
        return w

    # ---- actions ----
    def _do(self, label, fn, refresh=False):
        self.log.emit(f"{label}…")
        j = _Job(lambda: fn(self.handler))
        j.done.connect(lambda r: (self.log.emit(f"[OK] {label}"),
                                  self.refresh() if refresh else None))
        j.fail.connect(lambda m: self.log.emit(f"[ERROR] {label}: {m}"))
        j.finished.connect(lambda: self._jobs.remove(j) if j in self._jobs else None)
        self._jobs.append(j); j.start()

    def _dial(self):
        n = self.number.text().strip()
        if n:
            self._do(f"dial {n}", lambda h: h.dial(n, safe=False))

    def _call(self):
        n = self.number.text().strip()
        if n:
            self._do(f"call {n}", lambda h: h.call(n, safe=False))

    def _call_from_log(self, row, _col):
        it = self.calls.item(row, 1)
        if it:
            self.number.setText(it.text())

    def _send_sms(self):
        n = self.sms_to.text().strip(); b = self.sms_body.text()
        if n:
            self._do(f"sms to {n}", lambda h: h.send_sms(n, b, safe=False))

    # ---- loaders ----
    def refresh(self):
        self._do("call state", lambda h: h.call_state(safe=False))   # logs only
        self._load_state()
        self._load_calls()
        self._load_sms()

    def _load_state(self):
        j = _Job(lambda: self.handler.call_state(safe=False))
        j.done.connect(lambda s: self.state.setText(f"call: {s}"))
        j.fail.connect(lambda m: None)
        j.finished.connect(lambda: self._jobs.remove(j) if j in self._jobs else None)
        self._jobs.append(j); j.start()

    def _load_calls(self):
        j = _Job(lambda: self.handler.call_log(50, safe=False))
        j.done.connect(self._fill_calls)
        j.fail.connect(lambda m: self.log.emit("[ERROR] call log: " + m))
        j.finished.connect(lambda: self._jobs.remove(j) if j in self._jobs else None)
        self._jobs.append(j); j.start()

    def _fill_calls(self, rows):
        self.calls.setRowCount(0)
        for d in rows:
            r = self.calls.rowCount(); self.calls.insertRow(r)
            self.calls.setItem(r, 0, QTableWidgetItem(_CALL_TYPE.get(d.get("type"), d.get("type", ""))))
            self.calls.setItem(r, 1, QTableWidgetItem(d.get("number", "")))
            self.calls.setItem(r, 2, QTableWidgetItem(_when(d.get("date"))))
            self.calls.setItem(r, 3, QTableWidgetItem(_dur(d.get("duration"))))
        self.log.emit(f"[OK] {len(rows)} call(s)")

    def _load_sms(self):
        j = _Job(lambda: self.handler.sms_list(50, safe=False))
        j.done.connect(self._fill_sms)
        j.fail.connect(lambda m: self.log.emit("[ERROR] sms: " + m))
        j.finished.connect(lambda: self._jobs.remove(j) if j in self._jobs else None)
        self._jobs.append(j); j.start()

    def _fill_sms(self, rows):
        self.sms.setRowCount(0)
        for d in rows:
            r = self.sms.rowCount(); self.sms.insertRow(r)
            self.sms.setItem(r, 0, QTableWidgetItem(_SMS_TYPE.get(d.get("type"), d.get("type", ""))))
            self.sms.setItem(r, 1, QTableWidgetItem(d.get("address", "")))
            self.sms.setItem(r, 2, QTableWidgetItem(_when(d.get("date"))))
            self.sms.setItem(r, 3, QTableWidgetItem((d.get("body") or "").replace("\n", " ")))
        self.log.emit(f"[OK] {len(rows)} message(s)")

    def close_panel(self):
        for j in list(self._jobs):
            j.wait(700)
