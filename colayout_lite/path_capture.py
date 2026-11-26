"""Small view-only orthogonal point capture. No geometry/model publication."""
from PySide6.QtCore import QObject, QEvent, Qt, Signal


class PathCapture(QObject):
    active_changed = Signal(bool)

    def __init__(self, window, changed, finished):
        super().__init__(window)
        self.window = window
        self.changed = changed
        self.finished = finished
        self.active = False
        self.points = []
        self.cursor = None
        self.key = None
        window.canvas.viewport().installEventFilter(self)
        window.canvas.installEventFilter(self)

    def dispose(self):
        self.active = False
        try:
            self.window.canvas.viewport().removeEventFilter(self)
            self.window.canvas.removeEventFilter(self)
        except RuntimeError:
            pass

    def start(self):
        self.cancel()
        e = self.window.editor
        self.key = (id(e.w), e.scope_id)
        self.active = True
        self.active_changed.emit(True)
        self.window.canvas.setFocus()
        self.window.canvas.setCursor(Qt.CursorShape.CrossCursor)

    def cancel(self):
        self.active = False
        self.active_changed.emit(False)
        self.points = []
        self.cursor = None
        self.window.canvas.unsetCursor()
        self.changed([])

    def end(self):
        points = list(self.points)
        if len(points) < 2:
            return
        self.cancel()
        self.finished(points)

    def undo_step(self) -> bool:
        if not self.points:
            return False
        self.points.pop()
        self._emit()
        return True

    def right_click(self):
        if self.points:
            self.undo_step()
        else:
            self.cancel()

    def _emit(self):
        tail = [self.cursor] if self.cursor is not None and (not self.points or self.points[-1] != self.cursor) else []
        self.changed(self.points + tail)

    def handle_key(self, event) -> bool:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.end()
            return True
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
            return True
        if event.key() == Qt.Key.Key_Backspace or (
            event.key() == Qt.Key.Key_U and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            if self.points:
                self.undo_step()
            return True
        if event.key() == Qt.Key.Key_Delete:
            return True
        return False

    def eventFilter(self, obj, event):
        if not self.active:
            return False
        e = self.window.editor
        if self.window.busy or (id(e.w), e.scope_id) != self.key:
            self.cancel()
            return False
        kind = event.type()
        if kind in (
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.MouseButtonRelease,
        ):
            if obj is not self.window.canvas.viewport():
                return False
            canvas = self.window.canvas
            if getattr(canvas, '_pan_last', None) is not None:
                if kind == QEvent.Type.MouseMove and not (event.buttons() & canvas._pan_button):
                    canvas._end_pan()
                else:
                    return False
            if kind in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick,
            ) and event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
                return False
            if kind == QEvent.Type.MouseButtonRelease:
                return True

            pos = canvas.mapToScene(event.position().toPoint())
            grid = e.profile.grid
            point = (round(pos.x() / grid) * grid, round(-pos.y() / grid) * grid)
            if self.points:
                x, y = self.points[-1]
                xx, yy = point
                point = (xx, y) if abs(xx - x) >= abs(yy - y) else (x, yy)
            if self.cursor != point:
                self.cursor = point
                if hasattr(canvas, 'cursor_moved'):
                    canvas.cursor_moved.emit(point[0], point[1])
            if kind != QEvent.Type.MouseMove and event.button() == Qt.MouseButton.LeftButton:
                if not self.points or self.points[-1] != point:
                    self.points.append(point)
                if kind == QEvent.Type.MouseButtonDblClick:
                    self.end()
                    return True
            self._emit()
            return True
        if kind == QEvent.Type.KeyPress:
            return self.handle_key(event)
        return False
