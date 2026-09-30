"""Enable ``python -m turboadb.gui`` (as ``turboadb-gui``: on Windows the GUI
runs as TurboADB.exe, see :mod:`turboadb.launcher`)."""

from ..cli import launch_gui

if __name__ == "__main__":
    raise SystemExit(launch_gui())
