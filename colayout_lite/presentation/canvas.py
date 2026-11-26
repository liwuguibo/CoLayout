"""Canvas gestures and ordered overlays; no workspace persistence or bridge."""

from PySide6.QtCore import Qt, Signal, QRect, QPointF, QEvent
from PySide6.QtGui import QColor, QPainter, QContextMenuEvent
from PySide6.QtWidgets import QApplication, QGraphicsView
from ..preview import PreviewView


class Canvas(PreviewView):
    clicked_at = Signal(float, float)
    cursor_moved = Signal(float, float)
    cursor_left = Signal()
    inspection_clicked = Signal(float, float)
    region_picked = Signal(object)
    region_cancelled = Signal()
    object_activated = Signal(str)
    view_changed = Signal()
    peek_changed = Signal(int)
    inspection_clear = Signal()

    def __init__(self, scene):
        super().__init__(scene)
        self.setObjectName('layoutCanvas')
        self.setBackgroundBrush(QColor('#090c10'))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.region_select = False
        self.drc_overlay = None
        self.parasitic_overlay = None
        self.routing_overlay = None
        self.wire_overlay = None
        self.guard_overlay = None
        self.ruler_overlay = None
        self.blackbox_peek = 0
        self.inspection_overlay = None
        self.peek_modifiers = Qt.KeyboardModifier.ControlModifier
        self.human = None
        self.selection_ui = None
        self._normal_press = None
        self._pan_last = None
        self._pan_button = None
        self._pan_started = False
        self._suppress_context_menu = False
        self._preview_press = False
        from ..box_capture import BoxCapture

        self.box_capture = BoxCapture(self)
        self.view_changed.connect(self.box_capture.refresh)

    @property
    def region_select(self):
        return self._region_select

    @region_select.setter
    def region_select(self, value):
        self._region_select = bool(value)
        if not value and hasattr(self, 'box_capture'):
            self.box_capture.cancel()

    def _active_crosshair(self) -> bool:
        w = self.window()
        return bool(
            self.region_select
            or getattr(getattr(w, 'ruler', None), 'active', False)
            or getattr(getattr(getattr(w, 'wire_panel', None), 'capture', None), 'active', False)
            or getattr(getattr(getattr(w, 'guard_panel', None), 'capture', None), 'active', False)
        )

    def _end_pan(self):
        was_panning = (
            self._pan_last is not None
            or self._pan_button is not None
            or self._pan_started
        )
        self._pan_last = None
        self._pan_button = None
        self._pan_started = False
        if was_panning:
            if self._active_crosshair():
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.unsetCursor()

    def _handle_modal_right_click(self) -> bool:
        w = self.window()
        wire_capture = getattr(getattr(w, 'wire_panel', None), 'capture', None)
        if wire_capture is not None and getattr(wire_capture, 'active', False):
            wire_capture.right_click()
            return True
        guard_capture = getattr(getattr(w, 'guard_panel', None), 'capture', None)
        if guard_capture is not None and getattr(guard_capture, 'active', False):
            guard_capture.right_click()
            return True
        ruler = getattr(w, 'ruler', None)
        if ruler is not None and getattr(ruler, 'active', False):
            ruler.right_click()
            return True
        if self.region_select:
            self.region_select = False
            self.region_cancelled.emit()
            self.unsetCursor()
            return True
        return False

    def fitInView(self, rect, mode=Qt.AspectRatioMode.IgnoreAspectRatio):
        if self.selection_ui:
            self.selection_ui.cancel()
        if self.human:
            self.human.cancel()
        # Navigation gives this view its own scene rect. Every focus/fit entry
        # point must reset it, including existing DRC and render-view callers.
        self.setSceneRect(rect)
        super().fitInView(rect, mode)
        if hasattr(self, 'box_capture'):
            self.box_capture.refresh()

    def wheelEvent(self, event):
        if self._preview_press:
            self._preview_press = False
            self._normal_press = None
        if self.selection_ui:
            self.selection_ui.cancel()
        if self.human and self.human.armed:
            self.human.cancel()
            self._normal_press = None
        if event.modifiers() == self.peek_modifiers:
            raw = event.angleDelta().y() or event.pixelDelta().y() or event.angleDelta().x() or event.pixelDelta().x()
            if raw:
                level = max(0, min(2, self.blackbox_peek + (-1 if raw > 0 else 1)))
                if level != self.blackbox_peek:
                    self.blackbox_peek = level
                    self.peek_changed.emit(level)
                event.accept()
                return
        if event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
            from ..camera import pan_by

            raw = (
                event.angleDelta().y()
                or event.angleDelta().x()
                or event.pixelDelta().y()
                or event.pixelDelta().x()
            )
            if raw:
                pan_by(
                    self,
                    QPointF(
                        (max(-1200, min(1200, raw)) / 120.0)
                        * max(32.0, self.viewport().width() * 0.08),
                        0.0,
                    ),
                )
                self.box_capture.update(event.position())
                self.view_changed.emit()
                event.accept()
                return
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if not delta:
            return
        from ..camera import zoom_at

        self.wheel_activity()
        zoom_at(self, event.position(), 1.2 ** (max(-1200, min(1200, delta)) / 120))
        self.box_capture.update(event.position())
        self.view_changed.emit()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            if self._pan_last is not None:
                if self._pan_button is not None and event.buttons() & self._pan_button:
                    event.accept()
                    return
                self._end_pan()
            if event.button() == Qt.MouseButton.RightButton and self.box_capture.active:
                self.box_capture.cancel()
                self._suppress_context_menu = True
                event.accept()
                return
            self._preview_press = False
            self._normal_press = None
            if self.selection_ui:
                self.selection_ui.cancel()
            if self.human:
                self.human.cancel()
            self._pan_last = QPointF(event.position())
            self._pan_button = event.button()
            self._pan_started = event.button() == Qt.MouseButton.MiddleButton
            self._suppress_context_menu = False
            if self._pan_started:
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if self.region_select and event.button() == Qt.MouseButton.LeftButton:
            self.box_capture.begin(event.position())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._normal_press = event.position()
            pt = self.mapToScene(event.position().toPoint())
            self.clicked_at.emit(pt.x(), -pt.y())
            inspection = getattr(self.window(), 'inspection', None)
            if inspection and inspection.preview_hit(pt.x(), -pt.y()) and not event.modifiers():
                self._preview_press = True
                event.accept()
                return
            if self.human and self.human.begin(event):
                event.accept()
                return
            if self.selection_ui and self.selection_ui.begin(event):
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pt = self.mapToScene(event.position().toPoint())
        self.cursor_moved.emit(pt.x(), -pt.y())
        if self._pan_last is not None:
            if self._pan_button is None or not (event.buttons() & self._pan_button):
                self._end_pan()
            else:
                from ..camera import pan_by

                if not self._pan_started:
                    if (event.position() - self._pan_last).manhattanLength() < QApplication.startDragDistance():
                        event.accept()
                        return
                    self._pan_started = True
                    self._suppress_context_menu = True
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                pan_by(self, event.position() - self._pan_last)
                self._pan_last = QPointF(event.position())
                self.view_changed.emit()
                event.accept()
                return
        if self.box_capture.active:
            self.box_capture.update(event.position())
            event.accept()
            return
        if self._preview_press:
            event.accept()
            return
        if self.selection_ui and self.selection_ui.move(event):
            event.accept()
            return
        if self.human and self.human.move(event):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pan_last is not None and (
            event.button() == self._pan_button or not (event.buttons() & self._pan_button)
        ):
            is_right_click = (
                event.button() == Qt.MouseButton.RightButton
                and self._pan_button == Qt.MouseButton.RightButton
                and not self._pan_started
            )
            self._end_pan()
            if is_right_click:
                handled = self._handle_modal_right_click()
                if handled:
                    self._suppress_context_menu = True
                else:
                    self.customContextMenuRequested.emit(event.position().toPoint())
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._preview_press:
            self._preview_press = False
            if (
                self._normal_press is not None
                and (event.position() - self._normal_press).manhattanLength()
                < QApplication.startDragDistance()
            ):
                pt = self.mapToScene(self._normal_press.toPoint())
                self.inspection_clicked.emit(pt.x(), -pt.y())
            self._normal_press = None
            event.accept()
            return
        if self.box_capture.active:
            bounds = self.box_capture.finish(event.position())
            if bounds is not None:
                self.region_picked.emit(bounds)
            event.accept()
            return
        if self.selection_ui and self.selection_ui.finish(event):
            event.accept()
            self._normal_press = None
            return
        if self.human and self.human.finish(event):
            event.accept()
            self._normal_press = None
            return
        super().mouseReleaseEvent(event)
        if (
            self._normal_press is not None
            and (event.position() - self._normal_press).manhattanLength()
            < QApplication.startDragDistance()
        ):
            pt = self.mapToScene(self._normal_press.toPoint())
            self.inspection_clicked.emit(pt.x(), -pt.y())
        self._normal_press = None

    def leaveEvent(self, event):
        self.cursor_left.emit()
        super().leaveEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.ContextMenu:
            reason = event.reason()
            if reason == QContextMenuEvent.Reason.Mouse:
                # Always consume the native mouse ContextMenu event; the single
                # intentional menu emission is handled in mouseReleaseEvent so
                # Linux/X11 and Windows behave identically.
                self._suppress_context_menu = False
                event.accept()
                return True
            if reason == QContextMenuEvent.Reason.Keyboard and self._active_crosshair():
                event.accept()
                return True
        if event.type() in (
            QEvent.Type.WindowDeactivate,
            QEvent.Type.UngrabMouse,
            QEvent.Type.ApplicationDeactivate,
        ):
            if self._pan_last is not None:
                self._end_pan()
        return super().viewportEvent(event)

    def event(self, event):
        if event.type() in (
            QEvent.Type.WindowDeactivate,
            QEvent.Type.UngrabMouse,
            QEvent.Type.ApplicationDeactivate,
            QEvent.Type.FocusOut,
        ):
            if self._pan_last is not None:
                self._end_pan()
        return super().event(event)

    def focusOutEvent(self, event):
        if self._pan_last is not None:
            self._end_pan()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        self._end_pan()
        if event.key() == Qt.Key.Key_Escape and self.region_select:
            self.region_select = False
            self.region_cancelled.emit()
            self.unsetCursor()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self._normal_press = None
            self._preview_press = False
            self.inspection_clear.emit()
            event.accept()
            return
        if event.key() in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            from ..camera import pan_by

            ratio = (
                0.25
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                else 0.10
            )
            step = max(
                24.0,
                min(self.viewport().width(), self.viewport().height()) * ratio,
            )
            if event.key() == Qt.Key.Key_Right:
                pan_by(self, QPointF(-step, 0.0))
            elif event.key() == Qt.Key.Key_Left:
                pan_by(self, QPointF(step, 0.0))
            elif event.key() == Qt.Key.Key_Up:
                pan_by(self, QPointF(0.0, step))
            else:  # Down
                pan_by(self, QPointF(0.0, -step))
            self.box_capture.refresh()
            self.view_changed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.region_select:
            self.mousePressEvent(event)
            return
        self._preview_press = False
        from ..canvas_hits import local_module_at, native_module_at

        ident = local_module_at(self.window(), event.position())
        if ident:
            if self.human:
                self.human.cancel()
            self.object_activated.emit(ident)
            event.accept()
            return
        native = native_module_at(self.window(), event.position())
        if native:
            from PySide6.QtWidgets import QToolTip

            if self.human:
                self.human.cancel()
            if self.selection_ui:
                self.selection_ui.cancel()
            self._normal_press = None
            self.window().inspection.clear_trace()
            name = self.window().editor.w.layout_connectivity.instances[native].name
            message = f'{name} 是 Virtuoso 原生模块，在 CoLayout 中仅作黑盒使用，不能进入内部；请到 Virtuoso 编辑。'
            self.window().status.setText(message)
            QToolTip.showText(
                self.viewport().mapToGlobal(event.position().toPoint()),
                message,
                self.viewport(),
                QRect(),
                6500,
            )
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.parasitic_overlay:
            painter = QPainter(self.viewport())
            try:
                self.parasitic_overlay(painter)
            finally:
                painter.end()
        if self.routing_overlay:
            painter = QPainter(self.viewport())
            try:
                self.routing_overlay(painter)
            finally:
                painter.end()
        if self.wire_overlay:
            painter = QPainter(self.viewport())
            try:
                self.wire_overlay(painter)
            finally:
                painter.end()
        if self.drc_overlay:
            painter = QPainter(self.viewport())
            try:
                self.drc_overlay(painter)
            finally:
                painter.end()
        if self.inspection_overlay:
            painter = QPainter(self.viewport())
            try:
                self.inspection_overlay(painter)
            finally:
                painter.end()
        if self.selection_ui:
            painter = QPainter(self.viewport())
            try:
                self.selection_ui.paint(painter)
            finally:
                painter.end()
        if self.human:
            painter = QPainter(self.viewport())
            try:
                self.human.paint(painter)
            finally:
                painter.end()
        if self.guard_overlay:
            painter = QPainter(self.viewport())
            try:
                self.guard_overlay(painter)
            finally:
                painter.end()
        if self.ruler_overlay:
            painter = QPainter(self.viewport())
            try:
                self.ruler_overlay(painter)
            finally:
                painter.end()
