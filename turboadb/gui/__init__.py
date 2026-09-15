"""TurboADB PyQt5 desktop GUI.

    from turboadb.gui.app import main
    main()

Or run ``turboadb-gui`` after installing the ``gui`` extra, or
``python -m turboadb.gui``.
"""

from __future__ import annotations


def main():
    from .app import main as _main

    return _main()
