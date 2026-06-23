"""Small file helpers for the GUI: open a saved file, reveal it in the file
manager, and a 'Saved' popup offering both."""

from __future__ import annotations

import os
import sys
import subprocess


def open_path(path: str) -> None:
    """Open *path* with its default application."""
    try:
        if sys.platform == "win32":
            os.startfile(path)                       # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def reveal_path(path: str) -> None:
    """Show *path* selected in the OS file manager (Explorer / Finder / …)."""
    path = os.path.normpath(path)
    try:
        if sys.platform == "win32":
            # explorer needs the comma glued to /select and a quoted path
            subprocess.Popen('explorer /select,"%s"' % path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
    except Exception:
        pass


def saved_dialog(parent, path: str, what: str = "file") -> None:
    """After saving, show a popup with the path (as a clickable link) and
    Open file / Open folder / Close buttons."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMessageBox, QLabel
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle("Saved")
    box.setText(f"Saved {what}.")
    href = "file:///" + path.replace("\\", "/")
    box.setInformativeText(f'<a href="{href}">{path}</a>')
    box.setTextFormat(Qt.RichText)
    # make the file link actually clickable (opens the file in its default app)
    for lbl in box.findChildren(QLabel):
        lbl.setOpenExternalLinks(True)
        lbl.setTextInteractionFlags(lbl.textInteractionFlags()
                                    | Qt.TextBrowserInteraction)
    b_open = box.addButton("Open file", QMessageBox.AcceptRole)
    b_folder = box.addButton("Open folder", QMessageBox.ActionRole)
    box.addButton("Close", QMessageBox.RejectRole)
    box.setDefaultButton(b_open)
    box.exec_()
    clicked = box.clickedButton()
    if clicked is b_open:
        open_path(path)
    elif clicked is b_folder:
        reveal_path(path)
