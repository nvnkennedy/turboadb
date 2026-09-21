"""Readable, exportable device reports with a concise summary and raw dump."""

from __future__ import annotations

import html
import os
import re
import time

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QTextDocument
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .fileutil import (ask_save_path, copy_to_clipboard, save_output,
                       write_file_async, write_text_file)
from .icons import icon
from . import settings as settings_mod

_RAW_SEPARATOR = "--- RAW DIAGNOSTICS ---"
_ROW_RE = re.compile(r"^(.+?)\s{2,}(.+)$")


def _report_palette() -> dict[str, str]:
    """Return the report palette for the active UI theme.

    Reports are often saved and reviewed outside the app, so they carry their
    own colours instead of relying on the application's stylesheet.
    """
    from . import theme

    return theme.report_palette(settings_mod.get("theme", "dark"))


def _with_suffix(path: str, suffix: str) -> str:
    return path if os.path.splitext(path)[1] else path + suffix


def split_report(text: str) -> tuple[str, str]:
    """Separate a report's normal overview from optional diagnostic evidence."""
    summary, marker, detail = str(text or "").partition(_RAW_SEPARATOR)
    return summary.strip(), detail.strip() if marker else ""


def summary_html(title: str, summary: str, palette: dict[str, str] | None = None) -> str:
    """Turn grouped ``key  value`` report text into a compact HTML report."""
    palette = palette or _report_palette()
    blocks = []
    rows = []

    def flush_rows():
        nonlocal rows
        if not rows:
            return
        blocks.append(
            '<table class="report-table">'
            + "".join(f"<tr><th>{key}</th><td>{value}</td></tr>" for key, value in rows)
            + "</table>"
        )
        rows = []

    lines = summary.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if not line:
            flush_rows()
        elif next_line and set(next_line) == {"-"}:
            flush_rows()
            blocks.append(f"<h2>{html.escape(line)}</h2>")
            index += 1
        elif ":" in line and not line.startswith("["):
            key, value = line.split(":", 1)
            rows.append((html.escape(key.strip()), html.escape(value.strip())))
        else:
            match = _ROW_RE.match(line)
            if match:
                rows.append((html.escape(match.group(1).strip()), html.escape(match.group(2).strip())))
            else:
                flush_rows()
                blocks.append(f"<p>{html.escape(line)}</p>")
        index += 1
    flush_rows()
    body = "\n".join(blocks) or "<p>No report data was returned.</p>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
body {{ background:{palette['background']}; color:{palette['foreground']}; font:10.5pt 'Segoe UI',sans-serif; margin:28px; }}
h1 {{ color:{palette['accent']}; font-size:20pt; margin:0 0 16px; }}
h2 {{ color:{palette['accent']}; font-size:12pt; margin:20px 0 7px; padding-bottom:5px; border-bottom:2px solid {palette['border']}; }}
p {{ margin:7px 0; color:{palette['muted']}; }}
.report-table {{ width:100%; border-collapse:collapse; margin:0 0 12px; background:{palette['surface']}; }}
.report-table th {{ width:34%; text-align:left; background:{palette['header']}; color:{palette['foreground']}; padding:7px 10px; border:1px solid {palette['border']}; font-weight:600; }}
.report-table td {{ padding:7px 10px; border:1px solid {palette['border']}; color:{palette['foreground']}; font-family:'Cascadia Mono','Consolas',monospace; white-space:pre-wrap; }}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"""


def report_html(title: str, summary: str, detail: str) -> str:
    """Create a portable HTML report; raw evidence is available but collapsed."""
    palette = _report_palette()
    page = summary_html(title, summary, palette)
    if not detail:
        return page
    raw = (
        "<details><summary>Raw diagnostic dump</summary><pre>"
        + html.escape(detail)
        + "</pre></details>"
    )
    style = (
        f"<style>details{{margin-top:22px;padding:12px;background:{palette['surface']};"
        f"border:1px solid {palette['border']};}}summary{{cursor:pointer;color:{palette['accent']};"
        f"font-weight:600;}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;"
        f"font:9pt 'Cascadia Mono','Consolas',monospace;color:{palette['foreground']};}}</style>"
    )
    return page.replace("</head>", style + "</head>").replace("</body>", raw + "</body>")


def render_report_png(path: str, title: str, summary: str) -> None:
    """Render only the polished summary into a readable, support-ready PNG."""
    _save_image(render_report_image(title, summary), path)


def _save_image(image, path: str) -> None:
    if not image.save(path, "PNG"):
        raise OSError("could not save the PNG report")


def render_report_image(title: str, summary: str) -> QImage:
    """Render the summary into a QImage (call on the UI thread)."""
    width, margin = 1400, 32
    palette = _report_palette()
    doc = QTextDocument()
    doc.setHtml(summary_html(title, summary, palette))
    doc.setTextWidth(width - (margin * 2))
    height = max(180, int(doc.size().height()) + (margin * 2))
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise OSError("could not allocate image for the report")
    image.fill(QColor(palette["background"]))
    painter = QPainter(image)
    painter.translate(margin, margin)
    doc.drawContents(painter, QRectF(0, 0, width - (margin * 2), height - (margin * 2)))
    painter.end()
    return image


def show_report_dialog(parent, title: str, text: str, stem: str, description: str = "") -> None:
    """Show a table-based summary, optional raw dump, and useful exports."""
    summary, detail = split_report(text)
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(820, 600)
    root = QVBoxLayout(dlg)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)
    layout = QVBoxLayout()
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(8)
    root.addLayout(layout, 1)
    if description:
        hint = QLabel(description)
        hint.setObjectName("settingsHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
    tabs = QTabWidget()
    summary_view = QTextBrowser()
    summary_view.setOpenExternalLinks(False)
    palette = _report_palette()
    summary_view.setStyleSheet(
        f"QTextBrowser {{ background: {palette['background']}; color: {palette['foreground']}; "
        f"border: 1px solid {palette['border']}; }}"
    )
    summary_view.setHtml(summary_html(title, summary, palette))
    tabs.addTab(summary_view, icon("list", "blue"), "Summary")
    if detail:
        raw_view = QPlainTextEdit()
        raw_view.setReadOnly(True)
        font = QFont("Cascadia Mono", 9)
        font.setStyleHint(QFont.Monospace)
        raw_view.setFont(font)
        raw_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {palette['background']}; color: {palette['foreground']}; "
            f"border: 1px solid {palette['border']}; selection-background-color: {palette['header']}; }}"
        )
        raw_view.setPlainText(detail)
        tabs.addTab(raw_view, icon("terminal", "teal"), "Raw diagnostic dump")
    layout.addWidget(tabs, 1)

    footer = QWidget()
    footer.setObjectName("dialogFooter")
    footer.setAttribute(Qt.WA_StyledBackground, True)
    actions = QHBoxLayout(footer)
    actions.setContentsMargins(16, 10, 16, 10)
    actions.setSpacing(8)
    copy = QPushButton("Copy summary")
    copy.setProperty("role", "ghost")
    copy.setIcon(icon("copy", "blue"))
    copy.clicked.connect(lambda: copy_to_clipboard(copy, summary, "the summary"))
    actions.addWidget(copy)

    def stamped(extension):
        return f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}{extension}"

    def save_text():
        report_text = str(text).rstrip() + "\n"
        save_output(
            dlg, f"Save {title} as text", stamped(".txt"),
            lambda path: write_text_file(path, report_text, newline="\n"),
            what=f"{title} text", file_filter="Text files (*.txt);;All files (*)", suffix=".txt",
        )

    def save_html():
        page = report_html(title, summary, detail)
        save_output(
            dlg, f"Save {title} as HTML", stamped(".html"),
            lambda path: write_text_file(path, page, newline="\n"),
            what=f"{title} HTML report", file_filter="HTML files (*.html);;All files (*)",
            suffix=".html",
        )

    def save_png():
        path = ask_save_path(
            dlg, f"Save {title} summary as PNG", stamped(".png"), "PNG image (*.png)", ".png"
        )
        if not path:
            return
        try:
            # QTextDocument/QPainter rendering stays on the UI thread; only the
            # (reentrant) QImage encode + disk write moves to the worker.
            image = render_report_image(title, summary)
        except OSError as exc:
            from PyQt5.QtWidgets import QMessageBox

            QMessageBox.warning(dlg, "Save report", f"Could not render the PNG report:\n{exc}")
            return
        write_file_async(
            dlg, path, lambda target: _save_image(image, target),
            what=f"{title} summary PNG", title="Save report",
        )

    for label, glyph, slot in (("Save text…", "save", save_text), ("Save HTML…", "globe", save_html),
                               ("Save PNG…", "image", save_png)):
        button = QPushButton(label)
        button.setProperty("role", "ghost")
        button.setIcon(icon(glyph, "teal"))
        button.clicked.connect(slot)
        actions.addWidget(button)
    close = QPushButton("Close")
    close.setProperty("role", "ok")
    close.setIcon(icon("check", "on-accent"))
    close.setDefault(True)
    close.clicked.connect(dlg.accept)
    actions.addStretch(1)
    actions.addWidget(close)
    root.addWidget(footer)
    # Parented report dialogs (with their whole QTextDocument) used to stay
    # alive until the parent closed — one leak per report opened.
    dlg.setAttribute(Qt.WA_DeleteOnClose, True)
    dlg.exec_()
