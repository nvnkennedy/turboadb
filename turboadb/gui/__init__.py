"""TurboADB PyQt5 desktop GUI.

    from turboadb.gui.app import main
    main()

Or simply run ``turboadb-gui`` (prefers the bundled Windows exe), or
``python -m turboadb.gui``.
"""

from __future__ import annotations


def main():
    from .app import main as _main
    return _main()
