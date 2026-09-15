scripts/release.py copies dist/TurboADB-<version>-win64.exe here as turboadb-gui.exe
so the wheel ships it. turboadb-gui starts it when PyQt5 isn't installed.
The .exe itself is git-ignored.
