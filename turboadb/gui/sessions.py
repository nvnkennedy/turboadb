"""Saved device-target store with one validated on-disk format.

Targets are user-editable JSON, so all data is normalised at the boundary.  A
bad imported port must never make opening a device tab raise in the GUI.
"""

from __future__ import annotations

import json
import os
import re
import time
import warnings
from typing import Optional

from ..config import user_dir, user_path, validate_port
from .settings import _atomic_write_json, _file_lock, keep_unreadable

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
    """*value* checked as the engine checks every port; *default* when unset."""
    if value in (None, ""):
        return default
    return validate_port(value, field)


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


def _read_file(path: str, *, io_errors: bool = False):
    """Parse the store file: ``(targets, error_text_or_None)``. Never raises —
    an unreadable or invalid file yields no targets and a reason — except with
    *io_errors*, when a file that could not be opened or read at all raises
    its OSError (that says nothing about what the file holds)."""
    sessions, unreadable, invalid = _read_parts(path, io_errors=io_errors)
    return sessions, unreadable or invalid


def _read_parts(path: str, *, io_errors: bool = False):
    """:func:`_read_file` with its two kinds of error apart: ``(targets,
    unreadable, invalid)``, where *unreadable* says why the file as a whole
    is not a list of targets (None when it is) and *invalid* which of its
    entries were left out (None when none was)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_sessions = json.load(fh)
        if not isinstance(raw_sessions, list):
            raise ValueError("sessions file must contain a list of objects")
    except FileNotFoundError:
        return [], None, None
    except OSError as exc:
        if io_errors:
            raise
        return [], str(exc), None
    except (ValueError, json.JSONDecodeError) as exc:
        return [], str(exc), None
    sessions, errors = [], []
    for index, session in enumerate(raw_sessions, start=1):
        try:
            sessions.append(normalize_session(session))
        except ValueError as exc:
            errors.append(f"entry {index}: {exc}")
    return sessions, None, ("; ".join(errors) if errors else None)


# A read-modify-write tries a file it could not open this often.
_READ_ATTEMPTS = 6


def _retry_pause(attempt: int) -> None:
    time.sleep(0.05 * (attempt + 1))


def _read_for_update(path: str):
    """:func:`_read_parts` for a read-modify-write.  A file that could not be
    opened or read at all is tried again: taken for "no targets", the write
    after it replaced every saved target with the one being saved.  Raises
    OSError (nothing is written) when it keeps failing."""
    for attempt in range(_READ_ATTEMPTS):
        try:
            return _read_parts(path, io_errors=True)
        except OSError as exc:
            if attempt == _READ_ATTEMPTS - 1:
                raise OSError(
                    f"{path} could not be read ({exc}); the change was not saved") from exc
            _retry_pause(attempt)


def _keep_before_writing(path: str, error) -> None:
    """Before *path* is written, copy it aside as ``.corrupt-<time>``
    (:func:`keep_unreadable`) when it could not be read (*error*; None when
    it could): the write would otherwise drop what it holds.  Raises
    ValueError, and nothing may be written, when that copy failed."""
    if not error:
        return
    backup = keep_unreadable(path, error)
    if backup is None and os.path.exists(path):
        raise ValueError(
            f"{path} could not be read ({error}) or copied aside; it was left untouched")


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

    def _update(self, change) -> None:
        """Apply *change* to the targets ON DISK, write them, and adopt them.

        The file is the truth.  A long-running GUI holds the list it read at
        start-up while the CLI or another window may have added, renamed or
        deleted targets since; writing that snapshot back resurrected deleted
        targets and reverted their edits.  *change* edits the freshly read list
        in place, so only this call's own change is applied, and the whole
        read-change-write runs under the cross-process lock the settings file
        uses.  A file that can't be read is copied aside first
        (:func:`keep_unreadable`): writing would otherwise drop what it holds.
        One that can't even be opened (a sharing violation while a scanner has
        it) is tried again, and never taken for "no targets"."""
        path = sessions_file()
        with _file_lock(path):
            disk, unreadable, invalid = _read_for_update(path)
            _keep_before_writing(path, unreadable or invalid)
            change(disk)
            _atomic_write_json(path, disk)
            self.sessions = disk

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
        old = previous_name if previous_name and previous_name != name else None

        def change(targets):
            # The renamed entry keeps its place; when another window renamed
            # or deleted it meanwhile, this saves under the new name instead.
            index = next((i for i, s in enumerate(targets) if s["name"] == (old or name)), None)
            if index is None and old:
                index = next((i for i, s in enumerate(targets) if s["name"] == name), None)
            if index is None:
                targets.append(session)
            else:
                targets[index] = session
            # A rename onto another existing name replaces that target.
            targets[:] = [s for s in targets if s is session or s["name"] != name]

        self._update(change)

    def delete(self, name: str):
        def change(targets):
            targets[:] = [s for s in targets if s["name"] != name]

        self._update(change)

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
        read — writing the old list back resurrected them.  A file with some
        invalid entries is handled as :meth:`save` handles it: a copy is kept
        (``.corrupt-<time>``) and the valid targets are written back with the
        new one.  Refusing it left every connect logging "could not be read"
        and nothing was saved until the file was mended by hand.

        Raises ``ValueError`` for an invalid target, or when the file on disk
        is not a list of targets at all (a device that connected is no reason
        to replace it), and OSError when it cannot be opened.
        """
        session = normalize_session(session)
        identity = session_identity(session)
        identities = {identity}
        identities.update(item for item in also if item is not None)
        path = sessions_file()
        with _file_lock(path):
            disk, unreadable, invalid = _read_for_update(path)
            if unreadable:
                raise ValueError(
                    f"{path} could not be read ({unreadable}); it was left untouched"
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
                        _keep_before_writing(path, invalid)
                        self.sessions[index] = dict(existing, port=session["port"])
                        _atomic_write_json(path, self.sessions)
                        return "updated", dict(self.sessions[index])
            session["name"] = self.unique_name(session["name"])
            _keep_before_writing(path, invalid)
            self.sessions.append(session)
            _atomic_write_json(path, self.sessions)
        return "added", dict(session)

    def add_if_new(self, session: dict, *, also=()) -> Optional[dict]:
        """:meth:`remember`, returning the saved (added or updated) target, or
        None when the device was already saved."""
        outcome, target = self.remember(session, also=also)
        return None if outcome == "known" else target

    def export_to(self, path: str) -> int:
        """Write all saved targets to *path* as JSON. Returns the count.

        Exports what the file holds now (another window or the CLI may have
        changed it since this store read it), unless it can't be read."""
        disk, error = _read_file(sessions_file())
        if not error:
            self.sessions = disk
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

        def change(targets):
            for session in validated:
                for index, existing in enumerate(targets):
                    if existing["name"] == session["name"]:
                        targets[index] = session
                        break
                else:
                    targets.append(session)

        if validated:
            self._update(change)
        return len(validated)
