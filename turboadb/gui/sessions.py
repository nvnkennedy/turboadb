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
        # keep a stable position: replace in place if the name already exists,
        # otherwise append — editing a target no longer jumps it to the bottom
        name = session["name"]
        for i, s in enumerate(self.sessions):
            if s.get("name") == name:
                self.sessions[i] = session
                self._flush()
                return
        self.sessions.append(session)
        self._flush()

    def delete(self, name: str):
        self.sessions = [s for s in self.sessions if s.get("name") != name]
        self._flush()

    def export_to(self, path: str) -> int:
        """Write all saved targets to *path* as JSON. Returns the count."""
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.sessions, fh, indent=2)
        return len(self.sessions)

    def import_from(self, path: str) -> int:
        """Merge targets from a JSON file (same shape as sessions.json) into the
        store, de-duped by name (imported entries win). Returns how many were
        added or updated. Raises ValueError on a malformed file."""
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("not a TurboADB targets file (expected a JSON list)")
        added = 0
        for s in data:
            if isinstance(s, dict) and s.get("name"):
                self.save(dict(s))
                added += 1
        return added
