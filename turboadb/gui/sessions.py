"""Saved device-target store with one validated on-disk format.

Targets are user-editable JSON, so all data is normalised at the boundary.  A
bad imported port must never make opening a device tab raise in the GUI.
"""

from __future__ import annotations

import json
import re
import warnings
from typing import Optional

from ..config import user_dir, user_path
from .settings import _atomic_write_json, _file_lock

_DIR = user_dir()
_FILE = user_path("sessions.json")
# The import-time values, so an explicit override (tests, embedders) of the
# module-level names can be told apart from "nobody touched them".
_DEFAULT_DIR, _DEFAULT_FILE = _DIR, _FILE
_TYPES = {"usb", "network", "remote"}
# The " (2)" SessionStore.unique_name() appends to a name another target has.
_NUMBERED = re.compile(r" \(\d+\)$")


def sessions_file() -> str:
    """Path of sessions.json, resolved on call (see
    :func:`turboadb.config.user_dir`) so a changed HOME can't leave targets and
    settings in two different directories. An override of ``_FILE`` still wins."""
    return _FILE if _FILE != _DEFAULT_FILE else user_path("sessions.json")


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


def session_identity(session) -> Optional[tuple]:
    """The device a target points at, independent of its editable name.

    ``("usb", serial)``, ``("network", host, port)`` or
    ``("remote", adb_host, adb_port, serial)`` — hosts lower-cased and without
    IPv6 brackets, ports as text. An empty serial means "the only device".
    ``None`` for anything that is not a target dictionary.
    """
    if not isinstance(session, dict):
        return None
    kind = str(session.get("type") or "usb").strip().lower()
    serial = str(session.get("serial") or "").strip()
    if kind == "remote":
        return (
            "remote",
            str(session.get("adb_host") or "").strip().strip("[]").lower(),
            str(session.get("adb_port") or 5037),
            serial,
        )
    host = str(session.get("host") or "").strip().strip("[]").lower()
    if kind == "network" or host:
        return ("network", host, str(session.get("port") or 5555))
    return ("usb", serial)


def _read_file(path: str):
    """Parse the store file: ``(targets, error_text_or_None)``. Never raises —
    an unreadable or invalid file yields no targets and a reason."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_sessions = json.load(fh)
        if not isinstance(raw_sessions, list):
            raise ValueError("sessions file must contain a list of objects")
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [], str(exc)
    sessions, errors = [], []
    for index, session in enumerate(raw_sessions, start=1):
        try:
            sessions.append(normalize_session(session))
        except ValueError as exc:
            errors.append(f"entry {index}: {exc}")
    return sessions, ("; ".join(errors) if errors else None)


class SessionStore:
    def __init__(self):
        self.sessions: list[dict] = []
        self.load_error: Optional[str] = None
        self.load()

    def load(self):
        path = sessions_file()
        sessions, error = _read_file(path)
        self.sessions = sessions
        self.load_error = error
        if error and sessions:
            warnings.warn(
                f"TurboADB ignored invalid saved target(s) in {path}: {error}.",
                RuntimeWarning,
                stacklevel=2,
            )
        elif error:
            warnings.warn(
                f"TurboADB could not load saved targets from {path}: {error}. "
                "No targets were loaded.",
                RuntimeWarning,
                stacklevel=2,
            )

    def _flush(self, removed=()):
        """Write the store, keeping targets that appeared on disk meanwhile.

        A long-running GUI holds the list it read at start-up; rewriting that
        snapshot silently deleted every target the CLI or a second window had
        added since. Names this store touched win, *removed* names stay deleted,
        and everything else on disk is merged back in. The whole
        read-merge-write runs under the same cross-process lock the settings
        file uses."""
        path = sessions_file()
        with _file_lock(path):
            disk, _error = _read_file(path)
            known = {s["name"] for s in self.sessions} | set(removed)
            self.sessions = self.sessions + [s for s in disk if s["name"] not in known]
            _atomic_write_json(path, self.sessions)

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
                # the old name is gone on purpose — don't merge it back in
                self._flush(removed=(previous_name,))
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
        self._flush(removed=(name,))

    def find_identity(self, identity) -> Optional[dict]:
        """The first saved target pointing at *identity* (see
        :func:`session_identity`), or None."""
        if identity is None:
            return None
        for session in self.sessions:
            if session_identity(session) == identity:
                return dict(session)
        return None

    def unique_name(self, name: str) -> str:
        """*name*, or ``name (2)``, ``name (3)``… when a target already has it."""
        taken = {s["name"].casefold() for s in self.sessions}
        if name.casefold() not in taken:
            return name
        number = 2
        while f"{name} ({number})".casefold() in taken:
            number += 1
        return f"{name} ({number})"

    def remember(self, session: dict, *, also=()):
        """Save a connected device as a target unless one already points at it.

        Returns ``(outcome, target)``: ``"known"`` with the existing target,
        ``"updated"`` when a network target of the same device (same host and
        name) only had another port — Android's wireless debugging picks a new
        port each time, and one phone must not pile up as "(2)", "(3)"… — or
        ``"added"`` with the new target.

        Targets match by :func:`session_identity`, never by name; *also* lists
        further identities that count as already saved (the target a device
        tab was opened from, say). A new target whose name is taken by another
        device gets a numbered name instead of replacing that target.

        Under the same lock as every save, the FILE is the truth: this store's
        own edits were all written when they were made, while another window
        or the CLI may have renamed or deleted targets since this list was
        read — writing the old list back resurrected them.

        Raises ``ValueError`` for an invalid target, or when the file on disk
        cannot be read: rewriting it would drop the targets it still holds.
        """
        session = normalize_session(session)
        identity = session_identity(session)
        identities = {identity}
        identities.update(item for item in also if item is not None)
        path = sessions_file()
        with _file_lock(path):
            disk, error = _read_file(path)
            if error:
                raise ValueError(
                    f"{path} could not be read ({error}); it was left untouched"
                )
            self.sessions = disk
            for existing in disk:
                if session_identity(existing) in identities:
                    return "known", dict(existing)
            if session["type"] == "network":
                name = session["name"].casefold()
                for index, existing in enumerate(disk):
                    # The saved name may carry the number unique_name() gave it
                    # ("vivo V2318 (2)" when the same phone was saved over USB).
                    if (
                        existing["type"] == "network"
                        and session_identity(existing)[1] == identity[1]
                        and _NUMBERED.sub("", existing["name"]).casefold() == name
                    ):
                        self.sessions[index] = dict(existing, port=session["port"])
                        _atomic_write_json(path, self.sessions)
                        return "updated", dict(self.sessions[index])
            session["name"] = self.unique_name(session["name"])
            self.sessions.append(session)
            _atomic_write_json(path, self.sessions)
        return "added", dict(session)

    def add_if_new(self, session: dict, *, also=()) -> Optional[dict]:
        """:meth:`remember`, returning the saved (added or updated) target, or
        None when the device was already saved."""
        outcome, target = self.remember(session, also=also)
        return None if outcome == "known" else target

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
