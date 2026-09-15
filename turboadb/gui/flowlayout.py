"""A wrapping (flow) layout: lays items left-to-right and wraps to the next row
when they don't fit. Crucially its minimum width is just ONE item wide, so a
toolbar/button-row using it never forces the whole window to stay wide — which
is what let Win+Left/Right snap to a real half-screen and kept the ribbon's Exit
button on-screen."""

from __future__ import annotations

from PyQt5.QtCore import QPoint, QRect, QSize, Qt
from PyQt5.QtWidgets import QLayout


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, hspacing=6, vspacing=6):
        super().__init__(parent)
        self._items = []
        self._hspace = hspacing
        self._vspace = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    # --- QLayout plumbing ---
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    # --- the wrapping logic ---
    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            w, h = hint.width(), hint.height()
            next_x = x + w + self._hspace
            if next_x - self._hspace > area.right() and line_h > 0:
                x = area.x()
                y = y + line_h + self._vspace
                next_x = x + w + self._hspace
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(w, h)))
            x = next_x
            line_h = max(line_h, h)
        return y + line_h - rect.y() + m.bottom()


class ToolbarFlowLayout(FlowLayout):
    """A page toolbar that behaves like a QHBoxLayout while it fits and wraps
    onto more rows when it doesn't, so a long toolbar can never force the whole
    window wider than the screen.

    Supports the QHBoxLayout calls toolbars use: ``addWidget(w, stretch)``,
    ``addLayout(layout, stretch)``, ``addSpacing(px)`` and ``addStretch()``.
    On one row, items after the first ``addStretch()`` are right-aligned and
    items added with a stretch share the spare width.
    """

    def __init__(self, parent=None, hspacing=8, vspacing=6):
        super().__init__(parent, 0, hspacing, vspacing)
        self._split = None
        self._stretch = {}

    def setSpacing(self, spacing):
        self._hspace = spacing

    def addWidget(self, widget, stretch=0, alignment=None):
        super().addWidget(widget)
        if stretch:
            self._stretch[id(self._items[-1])] = stretch

    def addLayout(self, layout, stretch=0):
        self.addChildLayout(layout)
        self.addItem(layout)
        if stretch:
            self._stretch[id(layout)] = stretch

    def addSpacing(self, size):
        from PyQt5.QtWidgets import QSizePolicy, QSpacerItem

        self.addItem(QSpacerItem(size, 0, QSizePolicy.Fixed, QSizePolicy.Minimum))

    def addStretch(self, stretch=0):
        """Items added after the first call are right-aligned while the row fits."""
        if self._split is None:
            self._split = len(self._items)

    def takeAt(self, index):
        item = super().takeAt(index)
        if item is not None:
            self._stretch.pop(id(item), None)
            if self._split is not None and index < self._split:
                self._split -= 1
        return item

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        width = max(0, area.width())
        split = len(self._items) if self._split is None else self._split
        entries = [
            (index, item, item.sizeHint())
            for index, item in enumerate(self._items)
            if not item.isEmpty() or item.spacerItem() is not None
        ]
        if not entries:
            return m.top() + m.bottom()
        gap = self._hspace
        total = sum(hint.width() for _i, _item, hint in entries) + gap * (len(entries) - 1)
        if total <= width:
            rows = [entries]
        else:
            rows, row, used = [], [], 0
            for entry in entries:
                if entry[1].spacerItem() is not None and not row:
                    continue  # no leading spacing on a wrapped row
                w = entry[2].width()
                if row and used + gap + w > width:
                    rows.append(row)
                    row, used = [], 0
                    if entry[1].spacerItem() is not None:
                        continue
                used = used + gap + w if row else w
                row.append(entry)
            if row:
                rows.append(row)
        y = area.y()
        for number, row in enumerate(rows):
            line_h = max(hint.height() for _i, _item, hint in row)
            if not test_only:
                one_row = len(rows) == 1
                widths = {i: hint.width() for i, _item, hint in row}
                used = sum(widths.values()) + gap * (len(row) - 1)
                weights = {i: self._stretch.get(id(item), 0) for i, item, _hint in row}
                share = sum(weights.values())
                spare = max(0, width - used)
                if share and spare:
                    for i in widths:
                        widths[i] += spare * weights[i] // share
                left = [e for e in row if not one_row or e[0] < split]
                right = [e for e in row if one_row and e[0] >= split]
                x = area.x()
                for i, item, hint in left:
                    w = widths[i]
                    item.setGeometry(QRect(QPoint(x, y + (line_h - hint.height()) // 2),
                                           QSize(w, hint.height())))
                    x += w + gap
                x = area.x() + width
                for i, item, hint in reversed(right):
                    w = widths[i]
                    x -= w
                    item.setGeometry(QRect(QPoint(x, y + (line_h - hint.height()) // 2),
                                           QSize(w, hint.height())))
                    x -= gap
            y += line_h
            if number < len(rows) - 1:
                y += self._vspace
        return y - rect.y() + m.bottom()
