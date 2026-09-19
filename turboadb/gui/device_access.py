"""Making protected device files writable.

When the device refuses a file change (permission denied, a read-only file
system), :class:`WriteAccessDialog` asks which of ``adb root``, ``adb
disable-verity`` and ``adb remount`` to run. ``DeviceTab.make_files_writable``
shows it and runs the choice through ``ADBHandler.make_writable``; the text
checks here decide when to offer it.
"""

from __future__ import annotations

import posixpath
import re
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

# EACCES, EROFS and EPERM as adb, toybox and the shell print them.
_REFUSED_RE = re.compile(
    r"permission denied|read-only file system|operation not permitted", re.IGNORECASE
)

# Partitions that are read-only until ``adb remount``.
SYSTEM_PARTITIONS = ("/system", "/system_ext", "/vendor", "/product", "/odm", "/oem")


def is_permission_problem(text) -> bool:
    """True when *text* (an error or command output) says the device refused access."""
    return bool(text) and bool(_REFUSED_RE.search(str(text)))


def refusal_line(text) -> str:
    """The first line of terminal output where the device refused access, else ``""``."""
    if not text or not _REFUSED_RE.search(str(text)):
        return ""
    from ..results import strip_ansi

    for line in strip_ansi(str(text)).splitlines():
        if _REFUSED_RE.search(line):
            return line.strip()[:200]
    return ""


def on_system_partition(path: str) -> bool:
    """True for a path on a partition that ``adb remount`` makes writable."""
    if not path or not path.startswith("/"):
        return False
    path = posixpath.normpath(path)
    return any(path == part or path.startswith(part + "/") for part in SYSTEM_PARTITIONS)


def describe_status(status: Optional[dict]) -> str:
    """One line for the dialog, e.g. ``adbd: not root · userdebug build · verity enforcing``."""
    if not status:
        return "The device's root and verity state could not be read."
    bits = ["adbd: root" if status.get("root") else "adbd: not root"]
    if status.get("build_type"):
        bits.append(f"{status['build_type']} build")
    if status.get("verity"):
        bits.append(f"verity {status['verity']}")
    if status.get("bootloader"):
        bits.append(f"bootloader {status['bootloader']}")
    return "   ·   ".join(bits)


class WriteAccessDialog(QDialog):
    """Choose the steps that make device files writable (and whether to retry)."""

    def __init__(self, status: Optional[dict], *, path: str = "", error: str = "",
                 action: str = "", can_retry: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Make device files writable")
        self.setMinimumWidth(520)
        status = status or {}
        is_root = bool(status.get("root"))
        verity_off = status.get("verity") == "disabled"
        read_only = "read-only" in (error or "").lower()

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        if error:
            where = f" in {path}" if path else ""
            intro = QLabel(f"The device refused {action or 'the change'}{where}:")
            intro.setWordWrap(True)
            lay.addWidget(intro)
            detail = QLabel("\n".join(str(error).strip().splitlines()[:4]))
            detail.setObjectName("mutedHint")
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(detail)
        prompt = QLabel("Choose what TurboADB should run on the device:")
        prompt.setWordWrap(True)
        lay.addWidget(prompt)
        state = QLabel(describe_status(status or None))
        state.setObjectName("mutedHint")
        state.setWordWrap(True)
        lay.addWidget(state)

        self.chk_root = QCheckBox("adb root — restart adbd with root permissions")
        self.chk_root.setChecked(not is_root)
        if is_root:
            self.chk_root.setText("adb root — adbd already runs as root")
        elif status and not status.get("debuggable"):
            self.chk_root.setToolTip("This build is not debuggable, so adb root is usually refused.")
        self.chk_verity = QCheckBox(
            "adb disable-verity — let system partitions change (reboots the device)")
        self.chk_verity.setChecked(False)
        if verity_off:
            self.chk_verity.setText("adb disable-verity — verity is already disabled")
            self.chk_verity.setEnabled(False)
        self.chk_remount = QCheckBox(
            "adb remount — mount /system, /vendor and /product read-write")
        self.chk_remount.setChecked(read_only or on_system_partition(path) or not error)
        self.chk_reboot = QCheckBox("Reboot when a step needs it, then continue")
        self.chk_reboot.setChecked(True)
        for box in (self.chk_root, self.chk_verity, self.chk_remount, self.chk_reboot):
            lay.addWidget(box)
        self.chk_retry = None
        if can_retry:
            self.chk_retry = QCheckBox(f"Try {action or 'the change'} again afterwards")
            self.chk_retry.setChecked(True)
            lay.addWidget(self.chk_retry)
        if status.get("bootloader") == "locked":
            warning = QLabel("The bootloader is locked: disable-verity and remount usually "
                             "fail until it is unlocked.")
            warning.setObjectName("mutedHint")
            warning.setWordWrap(True)
            lay.addWidget(warning)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.btn_run = buttons.button(QDialogButtonBox.Ok)
        self.btn_run.setText("Run selected")
        self.btn_run.setProperty("role", "ok")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        for box in (self.chk_root, self.chk_verity, self.chk_remount):
            box.toggled.connect(self._sync_run)
        self._sync_run()

    def _sync_run(self, *_args) -> None:
        self.btn_run.setEnabled(any(box.isChecked() and box.isEnabled() for box in
                                    (self.chk_root, self.chk_verity, self.chk_remount)))

    def choices(self) -> dict:
        """The selected steps as ``ADBHandler.make_writable`` keywords, plus ``retry``."""
        return {
            "root": self.chk_root.isChecked(),
            "disable_verity": self.chk_verity.isEnabled() and self.chk_verity.isChecked(),
            "remount": self.chk_remount.isChecked(),
            "reboot": self.chk_reboot.isChecked(),
            "retry": bool(self.chk_retry is not None and self.chk_retry.isChecked()),
        }
