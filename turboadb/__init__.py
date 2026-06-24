"""
TurboADB
========

An Android **ADB + scrcpy** device toolkit for automotive/embedded Android
(Android Automotive OS / IVI head units) and general Android work. Wraps Google's
``adb`` and ``scrcpy`` behind one robust, structured API — usable as a Python
library, a CLI (``turboadb``), or a full PyQt5 desktop GUI (``turboadb-gui``).

Quick start
-----------
    from turboadb import ADBHandler, ADBConfig

    # USB (only device) — or serial="..." to pick one of several
    with ADBHandler() as dev:
        print(dev.shell("getprop ro.build.version.release").text)
        dev.push("app.apk", "/data/local/tmp/app.apk")
        dev.install("app.apk", grant_perms=True)
        dev.logcat(tag="ActivityManager", match=r"ANR|FATAL", on_line=print)

    # Network / Wi-Fi head unit
    with ADBHandler(ADBConfig(host="192.168.1.50", port=5555)) as hu:
        hu.mirror(max_size=1280)          # launch scrcpy

Every action returns a structured result (``CommandResult`` / ``TransferResult``
/ ``StreamResult``). Pass ``safe=True`` to get an ``OperationResult`` instead of
exceptions (ideal for GUIs). The raw adb path is always at ``dev.adb_path``.
"""

from __future__ import annotations

__version__ = "1.0.2"

from .config import ADBConfig, ScrcpyOptions
from .core import ADBHandler, ADBDevice, ShellSession, ForwardHandle
from .devices import (Device, list_devices, first_online, remote_devices,
                      start_shared_server, server_is_shared, install_startup,
                      uninstall_startup, open_firewall)
from .scrcpy import (launch_scrcpy, ScrcpySession, resolve_host,
                     is_remote_session, is_local_host)
from .tools import (find_adb, find_scrcpy, adb_available, scrcpy_available,
                    adb_version, diagnose, ADB_DOWNLOAD, SCRCPY_DOWNLOAD)
from .toolsdl import (fetch_tools, download_platform_tools, download_scrcpy,
                      tools_dir, check_updates, upgrade_tools)
from .results import (CommandResult, TransferResult, StreamResult,
                      OperationResult, strip_ansi)
from .exceptions import (
    ADBError,
    ADBNotFoundError,
    ADBConnectionError,
    ADBTimeoutError,
    ADBNotConnectedError,
    ADBCommandError,
    ADBTransferError,
    ADBInstallError,
    ScrcpyError,
)

__all__ = [
    "ADBHandler", "ADBDevice", "ADBConfig", "ScrcpyOptions", "ShellSession",
    "ForwardHandle", "Device", "list_devices", "first_online", "remote_devices",
    "start_shared_server", "server_is_shared", "install_startup",
    "uninstall_startup",
    "launch_scrcpy", "ScrcpySession", "resolve_host", "is_remote_session",
    "find_adb", "find_scrcpy", "adb_available", "scrcpy_available",
    "adb_version", "diagnose", "ADB_DOWNLOAD", "SCRCPY_DOWNLOAD",
    "fetch_tools", "download_platform_tools", "download_scrcpy", "tools_dir",
    "check_updates", "upgrade_tools",
    "CommandResult", "TransferResult", "StreamResult", "OperationResult",
    "strip_ansi",
    "ADBError", "ADBNotFoundError", "ADBConnectionError", "ADBTimeoutError",
    "ADBNotConnectedError", "ADBCommandError", "ADBTransferError",
    "ADBInstallError", "ScrcpyError", "__version__",
]
