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

if __name__ == "__main__":
    # If subcommands/flags are provided, run as CLI; otherwise launch desktop GUI
    if len(sys.argv) > 1:
        sys.exit(cli_main())
    else:
        sys.exit(launch_gui())
