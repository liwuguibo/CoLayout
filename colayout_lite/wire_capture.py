"""Interactive, uncommitted orthogonal wire drafts with L-bend posture & layer hotkeys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from PySide6.QtCore import QEvent, QObject, Qt, Signal

from .actions import wire_distance
from .model import Shape
from .parasitic_rc import GeometrySet, merge_geometry_sets, shape_geometry

POSTURE_ORDER: tuple[Literal['hv', 'vh', 'ortho'], ...] = ('hv', 'vh', 'ortho')
POSTURE_LABELS = {
    'hv': '先横后竖 (L)',
    'vh': '先竖后横 (L)',
    'ortho': '单段正交',
}


@dataclass(frozen=True)
class DraftRoute:
    points: tuple = ()
    layers: tuple = ()
    widths_um: tuple = ()
    via_definitions: tuple = ()
    cut_rows: int = 1
    cut_columns: int = 1
    net_intent: str | None = None

    def shapes(self, profile):
        if len(self.points) < 2:
            return ()
        if len(self.layers) != len(self.points) - 1 or len(self.widths_um) != len(self.layers):
            raise ValueError('Incomplete draft segment settings')
        result = []
        for index, (a, b) in enumerate(zip(self.points, self.points[1:])):
            if a == b or a[0] != b[0] and a[1] != b[1]:
                raise ValueError('Draft accepts nonzero orthogonal segments')
            layer = self.layers[index]
            result.append(
                Shape(
                    kind='path',
                    lpp=(layer, 'drawing'),
                    points=(a, b),
                    width=wire_distance(self.widths_um[index], profile, width=True),
                    style='truncateExtend',
                    net_intent=self.net_intent,
                )
            )
            if index and self.layers[index - 1] != layer:
                definition = (
                    self.via_definitions[index - 1]
                    if index - 1 < len(self.via_definitions)
                    else None
                )
                if not definition:
                    raise ValueError('Missing via definition for a layer transition')
                result.append(
                    Shape(
                        kind='stdVia',
                        origin=a,
                        via_definition=definition,
                        via_parameters={'cutRows': self.cut_rows, 'cutColumns': self.cut_columns},
                        net_intent=self.net_intent,
                    )
                )
        return tuple(result)

    def geometry(self, profile):
        rows = [
            shape_geometry(shape, profile, f'draft:{index}', component_id='draft', simple_series=True)
            for index, shape in enumerate(self.shapes(profile))
        ]
        return merge_geometry_sets(*rows) if rows else GeometrySet()


def _tail_points(
    start: tuple[int, int],
    target: tuple[int, int],
    posture: Literal['hv', 'vh', 'ortho'],
) -> tuple[tuple[int, int], ...]:
    x, y = start
    xx, yy = target
    if (xx, yy) == (x, y):
        return ()
    if posture == 'ortho':
        end = (xx, y) if abs(xx - x) >= abs(yy - y) else (x, yy)
        return () if end == start else (end,)
    if xx == x or yy == y:
        return ((xx, yy),)
    corner = (xx, y) if posture == 'hv' else (x, yy)
    return (corner, (xx, yy))


def _append_tail(
    draft: DraftRoute,
    tail: tuple[tuple[int, int], ...],
    *,
    layer: str | None,
    width_um: float | None,
    via_definition: str | None,
) -> DraftRoute:
    if not tail:
        return draft
    if not draft.points:
        return DraftRoute(
            (tail[-1],),
            (),
            (),
            (),
            draft.cut_rows,
            draft.cut_columns,
            draft.net_intent,
        )
    count = len(tail)
    first_via = (via_definition,) if len(draft.points) > 1 else ()
    internal_vias = (None,) * (count - 1)
    return DraftRoute(
        draft.points + tail,
        draft.layers + (layer,) * count,
        draft.widths_um + (width_um,) * count,
        draft.via_definitions + first_via + internal_vias,
        draft.cut_rows,
        draft.cut_columns,
        draft.net_intent,
    )


class WireCapture(QObject):
    active_changed = Signal(bool)

    def __init__(
        self,
        window,
        changed,
        finished,
        *,
        step_layer: Callable[[int], None] | None = None,
    ):
        super().__init__(window)
        self.window = window
        self.changed = changed
        self.finished = finished
        self.step_layer = step_layer
        self.active = False
        self.posture: Literal['hv', 'vh', 'ortho'] = 'hv'
        self.draft = DraftRoute()
        self._history: list[DraftRoute] = []
        self.cursor: tuple[int, int] | None = None
        self.key = None
        self.layer = None
        self.width_um = None
        self.via_definition = None
        window.canvas.viewport().installEventFilter(self)
        window.canvas.installEventFilter(self)

    def dispose(self):
        self.active = False
        try:
            self.window.canvas.viewport().removeEventFilter(self)
            self.window.canvas.removeEventFilter(self)
        except RuntimeError:
            pass

    def start(self, *, layer, width_um, via_definition, cut_rows, cut_columns, net_intent):
        self.cancel()
        editor = self.window.editor
        self.key = (id(editor.w), editor.scope_id)
        self.layer = layer
        self.width_um = width_um
        self.via_definition = via_definition
        self.draft = DraftRoute(
            cut_rows=cut_rows,
            cut_columns=cut_columns,
            net_intent=net_intent or None,
        )
        self._history.clear()
        self.active = True
        self.active_changed.emit(True)
        self.window.canvas.setFocus()
        self.window.canvas.setCursor(Qt.CursorShape.CrossCursor)

    def cancel(self):
        was_active = self.active
        self.active = False
        self.draft = DraftRoute()
        self._history.clear()
        self.cursor = None
        self.window.canvas.unsetCursor()
        if was_active:
            self.active_changed.emit(False)
        self.changed(None)

    def end(self):
        draft = self._current_draft()
        if len(draft.points) < 2:
            return
        self.active = False
        self.draft = DraftRoute()
        self._history.clear()
        self.cursor = None
        self.window.canvas.unsetCursor()
        self.finished(draft)
        self.active_changed.emit(False)

    def set_segment(self, layer, width_um, via_definition):
        self.layer = layer
        self.width_um = width_um
        self.via_definition = via_definition
        if self.active:
            self._emit()

    def set_via_parameters(self, rows, columns):
        self.draft = DraftRoute(
            self.draft.points,
            self.draft.layers,
            self.draft.widths_um,
            self.draft.via_definitions,
            rows,
            columns,
            self.draft.net_intent,
        )
        if self.active:
            self._emit()

    def cycle_posture(self) -> str:
        idx = POSTURE_ORDER.index(self.posture)
        self.posture = POSTURE_ORDER[(idx + 1) % len(POSTURE_ORDER)]
        if self.active:
            self._emit()
        return POSTURE_LABELS[self.posture]

    def active_delta_dbu(self) -> tuple[int, int, int] | None:
        """Return `(dx, dy, manhattan_or_seg_length_dbu)` from last committed anchor."""
        if not self.active or not self.draft.points or self.cursor is None:
            return None
        tail = _tail_points(self.draft.points[-1], self.cursor, self.posture)
        if not tail:
            return (0, 0, 0)
        end = tail[-1]
        dx = end[0] - self.draft.points[-1][0]
        dy = end[1] - self.draft.points[-1][1]
        pts = (self.draft.points[-1], *tail)
        seg_len = sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(pts, pts[1:]))
        return (dx, dy, seg_len)

    def _current_draft(self) -> DraftRoute:
        if not self.cursor:
            return self.draft
        if not self.draft.points:
            return self.draft
        tail = _tail_points(self.draft.points[-1], self.cursor, self.posture)
        return _append_tail(
            self.draft,
            tail,
            layer=self.layer,
            width_um=self.width_um,
            via_definition=self.via_definition,
        )

    def _emit(self):
        self.changed(self._current_draft())

    def undo_step(self) -> bool:
        if not self.draft.points:
            return False
        if self._history:
            self.draft = self._history.pop()
        else:
            self.draft = DraftRoute(
                self.draft.points[:-1],
                self.draft.layers[:-1] if self.draft.layers else (),
                self.draft.widths_um[:-1] if self.draft.widths_um else (),
                self.draft.via_definitions[:-1] if self.draft.via_definitions else (),
                self.draft.cut_rows,
                self.draft.cut_columns,
                self.draft.net_intent,
            )
        if not self.draft.points:
            self.cursor = None
        self._emit()
        return True

    def right_click(self):
        if self.draft.points:
            self.undo_step()
        else:
            self.cancel()

    def handle_key(self, event) -> bool:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.end()
            return True
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
            return True
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            label = self.cycle_posture()
            if hasattr(self.window, 'status'):
                self.window.status.setText(f'走线折线姿态：{label}（Space 切换）')
            return True
        if event.key() in (Qt.Key.Key_BracketLeft, Qt.Key.Key_PageDown) and self.step_layer:
            self.step_layer(-1)
            return True
        if event.key() in (Qt.Key.Key_BracketRight, Qt.Key.Key_PageUp) and self.step_layer:
            self.step_layer(1)
            return True
        if event.key() == Qt.Key.Key_Backspace or (
            event.key() == Qt.Key.Key_U
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            if self.draft.points:
                self.undo_step()
            return True
        if event.key() == Qt.Key.Key_Delete:
            return True
        return False

    def eventFilter(self, obj, event):
        if not self.active:
            return False
        editor = self.window.editor
        if not editor or self.window.busy or (id(editor.w), editor.scope_id) != self.key:
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
                if kind == QEvent.Type.MouseMove and not (
                    event.buttons() & canvas._pan_button
                ):
                    canvas._end_pan()
                else:
                    return False
            if kind in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick,
            ) and event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
                return False

            position = canvas.mapToScene(event.position().toPoint())
            if hasattr(canvas, 'cursor_moved'):
                canvas.cursor_moved.emit(position.x(), -position.y())
            if kind == QEvent.Type.MouseButtonRelease:
                return True

            grid = editor.profile.grid
            point = (round(position.x() / grid) * grid, round(-position.y() / grid) * grid)
            self.cursor = point
            if kind != QEvent.Type.MouseMove:
                if event.button() == Qt.MouseButton.LeftButton:
                    if not self.draft.points:
                        self._history.append(self.draft)
                        self.draft = _append_tail(
                            self.draft,
                            (point,),
                            layer=self.layer,
                            width_um=self.width_um,
                            via_definition=self.via_definition,
                        )
                    else:
                        tail = _tail_points(self.draft.points[-1], point, self.posture)
                        if tail:
                            self._history.append(self.draft)
                            self.draft = _append_tail(
                                self.draft,
                                tail,
                                layer=self.layer,
                                width_um=self.width_um,
                                via_definition=self.via_definition,
                            )
                            self.cursor = tail[-1]
                    if kind == QEvent.Type.MouseButtonDblClick:
                        self.end()
                        return True
            self._emit()
            return True
        if kind == QEvent.Type.KeyPress:
            return self.handle_key(event)
        return False
