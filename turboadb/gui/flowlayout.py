"""A wrapping (flow) layout: lays items left-to-right and wraps to the next row
when they don't fit. Crucially its minimum width is just ONE item wide, so a
toolbar/button-row using it never forces the whole window to stay wide — which
is what let Win+Left/Right snap to a real half-screen and kept the ribbon's Exit
button on-screen."""

from __future__ import annotations

from PyQt5.QtCore import QPoint, QRect, QSize, Qt
from PyQt5.QtWidgets import QLayout, QSizePolicy


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
