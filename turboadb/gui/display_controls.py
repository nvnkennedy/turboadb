"""Basic device controls for ONE display: the row in each IVI display tile.

Back, Home and Recents go to that display (``input -d <id> keyevent``), so the
cluster or passenger screen gets its own navigation instead of whatever
display Android considers focused. Volume and power keys are device-wide on
Android; they are sent the same way. Presses run in order on the device
command dispatcher, off the UI thread; a failure is reported on :attr:`log`.
"""

from __future__ import annotations

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QToolButton, QWidget

from ..results import OperationResult
from .icons import icon

# (label, icon, key name from ADBHandler.KEYS, tone). Navigation is blue and
# volume purple, as on the Device Control page; power is red.
DISPLAY_KEYS = (
    ("Back", "back", "back", "blue"),
    ("Home", "home", "home", "blue"),
    ("Recents", "recents", "recents", "blue"),
    ("Volume down", "volume-down", "vol_down", "purple"),
    ("Volume up", "volume-up", "vol_up", "purple"),
    ("Mute", "mute", "vol_mute", "purple"),
    ("Power / sleep", "power", "power", "red"),
)


class DisplayControls(QWidget):
    """A compact row of icon buttons driving one display.

    *dispatcher* is the device tab's shared :class:`DeviceCommandDispatcher`
    (strict FIFO), so a Back pressed on the cluster never overtakes a key typed
    a moment earlier on the centre screen. *on_screenshot* is called by the
    Screenshot button.
    """

    log = pyqtSignal(str)
    MAX_PENDING = 16  # presses waiting for a slow device before more are dropped

    def __init__(self, handler, display_id, dispatcher, on_screenshot=None, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.display_id = int(display_id or 0)
        self._dispatcher = dispatcher
        self._pending = 0
        self._warned_full = False
        self.buttons = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        for label, icon_name, key, tone in DISPLAY_KEYS:
            button = self._button(label, icon_name, tone)
            button.clicked.connect(lambda _checked=False, k=key, t=label: self.send_key(k, t))
            self.buttons[key] = button
            row.addWidget(button)
        if on_screenshot is not None:
            shot = self._button("Screenshot of this display", "camera", "text")
            shot.clicked.connect(lambda _checked=False: on_screenshot())
            self.buttons["screenshot"] = shot
            row.addWidget(shot)
        self._sync_tips()

    @staticmethod
    def _button(label, icon_name, tone):
        button = QToolButton()
        button.setObjectName("iconButton")
        button.setIcon(icon(icon_name, tone))
        button.setIconSize(QSize(16, 16))
        button.setText(label)
        button.setAccessibleName(label)
        button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        button.setCursor(Qt.PointingHandCursor)
        button.setFocusPolicy(Qt.NoFocus)  # never pull the keyboard off the screen
        return button

    def set_display_id(self, display_id) -> None:
        self.display_id = int(display_id or 0)
        self._sync_tips()

    def _sync_tips(self) -> None:
        for label, _icon_name, key, _tone in DISPLAY_KEYS:
            where = (
                "on this device"
                if key in ("vol_down", "vol_up", "vol_mute", "power")
                else f"on display {self.display_id}"
            )
            self.buttons[key].setToolTip(f"{label} {where}")

    @property
    def pending(self) -> int:
        return self._pending

    def send_key(self, key, label=None) -> bool:
        """Queue one key press for this display; False when it was not queued."""
        label = label or str(key)
        if self._pending >= self.MAX_PENDING:
            if not self._warned_full:
                self._warned_full = True
                self.log.emit(
                    f"[WARNING] display {self.display_id}: the device is not keeping "
                    "up, so some button presses were dropped"
                )
            return False
        handler = self.handler
        # display 0 is the default display: no -d (older Android has none)
        extra = {"display_id": self.display_id} if self.display_id else {}

        def run():
            return handler.keyevent(key, safe=True, **extra)

        settled = []

        def finished():
            if settled:
                return
            settled.append(True)
            self._pending = max(0, self._pending - 1)
            if not self._pending:
                self._warned_full = False

        def done(result):
            finished()
            error = None
            if isinstance(result, OperationResult):
                if not result.success:
                    error = result.error or "failed"
                elif result.value is False:
                    error = "the device rejected it"
            elif result is False:
                error = "the device rejected it"
            if error:
                self.log.emit(f"[WARNING] display {self.display_id} {label}: {error}")

        def failed(message):
            finished()
            self.log.emit(f"[WARNING] display {self.display_id} {label}: {message}")

        self._pending += 1
        if not self._dispatcher.submit(run, on_done=done, on_fail=failed, priority=0):
            finished()  # refused (the dispatcher reports why through on_fail)
            return False
        return True
