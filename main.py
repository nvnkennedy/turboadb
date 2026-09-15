"""Convenience entry point for local testing and development.

Usage:
    python main.py              # Launches the full TurboADB PyQt5 desktop GUI
    python main.py devices      # Runs TurboADB CLI commands
    python main.py doctor
"""

import os
import sys

# Ensure local repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from turboadb.cli import launch_gui, main as cli_main


def main() -> int:
    """Run the local checkout without needing an editable install.

    ``python main.py`` launches the GUI; any CLI arguments keep their normal
    TurboADB meaning.  ``python main.py gui`` and ``--gui`` are explicit GUI
    aliases, which is handy for local smoke tests and IDE run configurations.
    """
    args = sys.argv[1:]
    if not args or args in (["gui"], ["--gui"]):
        return launch_gui()
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
