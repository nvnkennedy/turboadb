"""PyInstaller entry point. Imports the GUI as a package module (so relative
imports resolve) and launches it. Any *startup* crash (e.g. a missing import in
the frozen exe) is caught and shown in a native popup + written to a crash log,
so the exe never fails silently."""

import os
import sys
import traceback


def _fatal(message: str):
    try:
        d = os.path.join(os.path.expanduser("~"), ".turboadb")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "crash.log"), "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, message[:1800], "TurboADB GUI failed to start", 0x10)
    except Exception:
        sys.stderr.write(message + "\n")


def _selftest_winrm():
    """Import the whole pywinrm/NTLM stack in the FROZEN exe and write the result
    to ~/.turboadb/winrm-selftest.txt (a windowed exe has no visible stdout)."""
    import importlib
    bad = []
    # pywinrm 0.5 NTLM uses 'spnego' (not the old 'ntlm_auth')
    for m in ("winrm", "requests", "requests_ntlm", "spnego", "xmltodict",
              "cryptography"):
        try:
            importlib.import_module(m)
        except Exception as exc:
            bad.append(f"{m}: {exc}")
    try:
        import winrm
        winrm.Session("http://x:5985/wsman", auth=("D\\u", "p"), transport="ntlm")
        sess = "session-build OK"
    except Exception as exc:
        sess = f"session FAILED: {exc}"
        bad.append(sess)
    out = "WINRM-SELFTEST: " + ("ALL OK | " + sess if not bad
                                else "PROBLEMS -> " + " || ".join(bad))
    try:
        d = os.path.join(os.path.expanduser("~"), ".turboadb")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "winrm-selftest.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(out + "\n")
    except Exception:
        pass
    sys.stderr.write(out + "\n")
    return 0 if not bad else 2


def run():
    if os.environ.get("TURBOADB_SELFTEST") == "winrm":
        return _selftest_winrm()
    try:
        from turboadb.gui.app import main
        return main()
    except Exception:
        _fatal(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
