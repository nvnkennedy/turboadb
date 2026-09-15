"""Saved device-target store with one validated on-disk format.

Targets are user-editable JSON, so all data is normalised at the boundary.  A
bad imported port must never make opening a device tab raise in the GUI.
"""

from __future__ import annotations

import json
import os
import warnings
from typing import Optional

from .settings import _atomic_write_json

_DIR = os.path.join(os.path.expanduser("~"), ".turboadb")
_FILE = os.path.join(_DIR, "sessions.json")
_TYPES = {"usb", "network", "remote"}


def _text(value, field: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    return value


def _port(value, field: str, default: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a port number")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a port number") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{field} must be between 1 and 65535")
    return port


def normalize_session(session: dict) -> dict:
    """Return a safe canonical target dictionary or raise ``ValueError``.

    The defaults retain compatibility with older saved sessions that omitted a
    port or type, while invalid endpoint data is rejected before it reaches
    ``ADBConfig``.
    """
    if not isinstance(session, dict):
        raise ValueError("a saved target must be an object")

    name = _text(session.get("name"), "name", required=True)
    kind = _text(session.get("type", "usb"), "type", required=True).lower()
    if kind not in _TYPES:
        raise ValueError("type must be usb, network, or remote")

    normalized = {"name": name, "type": kind}
    if kind == "usb":
        normalized["serial"] = _text(session.get("serial"), "serial")
    elif kind == "network":
        normalized["host"] = _text(session.get("host"), "host", required=True)
        normalized["port"] = _port(session.get("port"), "port", 5555)
    else:
        normalized["adb_host"] = _text(
            session.get("adb_host"), "adb_host", required=True
        )
        normalized["adb_port"] = _port(session.get("adb_port"), "adb_port", 5037)
        normalized["serial"] = _text(session.get("serial"), "serial")
    return normalized


class SessionStore:
    def __init__(self):
        self.sessions: list[dict] = []
        self.load_error: Optional[str] = None
        self.load()

    def load(self):
        try:
            with open(_FILE, "r", encoding="utf-8") as fh:
                raw_sessions = json.load(fh)
            if not isinstance(raw_sessions, list):
                raise ValueError("sessions file must contain a list of objects")

            sessions = []
            errors = []
            for index, session in enumerate(raw_sessions, start=1):
                try:
                    sessions.append(normalize_session(session))
                except ValueError as exc:
                    errors.append(f"entry {index}: {exc}")
            self.sessions = sessions
            self.load_error = "; ".join(errors) if errors else None
            if errors:
                warnings.warn(
                    f"TurboADB ignored invalid saved target(s) in {_FILE}: "
                    f"{self.load_error}.",
                    RuntimeWarning,
                    stacklevel=2,
                )
        except FileNotFoundError:
            self.sessions = []
            self.load_error = None
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.sessions = []
            self.load_error = str(exc)
            warnings.warn(
                f"TurboADB could not load saved targets from {_FILE}: {exc}. "
                "No targets were loaded.",
                RuntimeWarning,
                stacklevel=2,
            )

    def _flush(self):
        _atomic_write_json(_FILE, self.sessions)

    def names(self) -> list:
        return [s["name"] for s in self.sessions]

    def get(self, name: str) -> Optional[dict]:
        for session in self.sessions:
            if session["name"] == name:
                return dict(session)
        return None

    def save(self, session: dict, previous_name: Optional[str] = None):
        """Create or replace a target.

        When *previous_name* (or a ``"previous_name"`` key, as returned by
        ``SessionDialog.result_session()`` for an edited target) names a
        different existing target, the target is RENAMED in place instead of
        leaving the old entry behind as an accidental copy.
        """
        if previous_name is None and isinstance(session, dict):
            previous_name = session.get("previous_name")
        session = normalize_session(session)
        name = session["name"]
        if previous_name and previous_name != name:
            old_index = next(
                (i for i, s in enumerate(self.sessions) if s["name"] == previous_name), None
            )
            if old_index is not None:
                self.sessions[old_index] = session
                # A rename onto another existing name replaces that target.
                self.sessions = [
                    s for i, s in enumerate(self.sessions)
                    if i == old_index or s["name"] != name
                ]
                self._flush()
                return
        for index, existing in enumerate(self.sessions):
            if existing["name"] == name:
                self.sessions[index] = session
                self._flush()
                return
        self.sessions.append(session)
        self._flush()

    def delete(self, name: str):
        self.sessions = [s for s in self.sessions if s["name"] != name]
        self._flush()

    def export_to(self, path: str) -> int:
        """Write all saved targets to *path* as JSON. Returns the count."""
        _atomic_write_json(path, self.sessions)
        return len(self.sessions)

    def import_from(self, path: str) -> int:
        """Merge a validated targets file, atomically from the user's view.

        Every entry is checked before any existing target is replaced.  This
        avoids a partially imported file leaving a later GUI action to fail.
        """
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("not a TurboADB targets file (expected a JSON list)")
        validated = []
        for index, session in enumerate(data, start=1):
            try:
                validated.append(normalize_session(session))
            except ValueError as exc:
                raise ValueError(f"invalid target at entry {index}: {exc}") from exc

        for session in validated:
            name = session["name"]
            for index, existing in enumerate(self.sessions):
                if existing["name"] == name:
                    self.sessions[index] = session
                    break
            else:
                self.sessions.append(session)
        if validated:
            self._flush()
        return len(validated)
