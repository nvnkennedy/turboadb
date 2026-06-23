"""Saved-target store (device profiles) at ~/.turboadb/sessions.json.

A target is just a small dict: name, type (usb/network), serial, host, port.
No secrets are involved with adb, so everything lives in one JSON file.
"""

from __future__ import annotations

import os
import json
from typing import Optional

_DIR = os.path.join(os.path.expanduser("~"), ".turboadb")
_FILE = os.path.join(_DIR, "sessions.json")


class SessionStore:
    def __init__(self):
        self.sessions: list = []
        self.load()

    def load(self):
        try:
            with open(_FILE, "r", encoding="utf-8") as fh:
                self.sessions = json.load(fh)
        except Exception:
            self.sessions = []

    def _flush(self):
        os.makedirs(_DIR, exist_ok=True)
        with open(_FILE, "w", encoding="utf-8") as fh:
            json.dump(self.sessions, fh, indent=2)

    def names(self) -> list:
        return [s.get("name", "?") for s in self.sessions]

    def get(self, name: str) -> Optional[dict]:
        for s in self.sessions:
            if s.get("name") == name:
                return dict(s)
        return None

    def save(self, session: dict):
        name = session["name"]
        self.sessions = [s for s in self.sessions if s.get("name") != name]
        self.sessions.append(session)
        self._flush()

    def delete(self, name: str):
        self.sessions = [s for s in self.sessions if s.get("name") != name]
        self._flush()
