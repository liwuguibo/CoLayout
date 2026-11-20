"""Pure-view interactive ruler overlay (`K` / `Shift+K`).

Rulers are strictly transient viewport annotations scoped to `(workspace.id, scope_id)`.
They never enter `Workspace`, `Editor.operations`, undo history, DRC, or sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot, log10, floor
from typing import Literal

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPen

from .drawing_mode import cancel_drawing
from .fonts import ui_font
from .render import NativeFigureItem, PreviewRectItem

MAX_RULERS_PER_SCOPE = 64


@dataclass(frozen=True)
class RulerSegment:
    p0: tuple[int, int]
    p1: tuple[int, int]
    mode: Literal['orthogonal', 'diagonal'] = 'orthogonal'


def format_um(dbu: float, scale: int) -> str:
    val = dbu / max(scale, 1)
    if abs(val) < 1e-9:
        return '0'
    text = f'{val:.4f}'.rstrip('0').rstrip('.')
    return text if text != '-0' else '0'


def _nice_tick_step_dbu(scale_px_per_dbu: float, dbu_per_micron: int) -> float | None:
    target_dbu = 56.0 / max(scale_px_per_dbu, 1e-12)
    target_um = target_dbu / max(dbu_per_micron, 1)
    if target_um <= 0:
        return None
    exp = floor(log10(target_um))
    base = 10.0 ** exp
    for mult in (1.0, 2.0, 5.0, 10.0):
        step_um = mult * base
        if step_um >= target_um:
            return step_um * dbu_per_micron
    return 10.0 * base * dbu_per_micron


class RulerTool(QObject):
    active_changed = Signal(bool)
    changed = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.active = False
        self.mode: Literal['orthogonal', 'diagonal'] = 'orthogonal'
        self.p0: tuple[int, int] | None = None
        self.cursor: tuple[int, int] | None = None
        self._snapped_edge = False
        self._scope_key: tuple[str, str | None] | None = None
        self._rulers_by_scope: dict[tuple[str, str | None], list[RulerSegment]] = {}
        window.canvas.ruler_overlay = self.paint
        window.canvas.viewport().installEventFilter(self)
        window.canvas.installEventFilter(self)

    def dispose(self):
        self.active = False
        try:
            self.window.canvas.viewport().removeEventFilter(self)
            self.window.canvas.removeEventFilter(self)
        except RuntimeError:
            pass

    def _current_key(self) -> tuple[str, str | None] | None:
        e = self.window.editor
        if not e:
            return None
        return (e.w.id, e.scope_id)

    def current_rulers(self) -> list[RulerSegment]:
        key = self._current_key()
        if key is None:
            return []
        return list(self._rulers_by_scope.get(key, ()))

    def active_delta_dbu(self) -> tuple[int, int] | None:
        if self.active and self.p0 is not None and self.cursor is not None:
            return (self.cursor[0] - self.p0[0], self.cursor[1] - self.p0[1])
        return None

    def start_or_toggle(self, *_):
        if self.active:
            self.cancel_capture()
            self.window.status.setText('已退出标尺测距（已放置标尺保留；Shift+K 清除）。')
            return
        if not self.window.editor or self.window.busy:
            return
        cancel_drawing(self.window, self)
        self.window.selection_ui.set_ids([])
        self._scope_key = self._current_key()
        self.p0 = None
        self.cursor = None
        self._snapped_edge = False
        self.active = True
        self.window.canvas.setFocus()
        self.window.canvas.setCursor(Qt.CursorShape.CrossCursor)
        self.active_changed.emit(True)
        self.changed.emit()
        self.window.view_state_changed.emit()
        self.window.canvas.viewport().update()
        mode_label = '正交' if self.mode == 'orthogonal' else '斜向'
        self.window.status.setText(
            f'标尺测距（{mode_label}）：单击起点与终点放置 · Space 切换正交/斜向 · 按住 Shift 纯网格吸附 · Esc 退出'
        )

    def cancel_capture(self, *_):
        was_active = self.active
        self.active = False
        self.p0 = None
        self.cursor = None
        self._snapped_edge = False
        if was_active:
            self.window.canvas.unsetCursor()
            self.active_changed.emit(False)
            self.changed.emit()
            self.window.view_state_changed.emit()
            self.window.canvas.viewport().update()

    cancel_draw = cancel_capture

    def clear_all(self, *_):
        self.cancel_capture()
        key = self._current_key()
        removed = 0
        if key is not None:
            removed = len(self._rulers_by_scope.pop(key, ()))
        else:
            removed = sum(len(v) for v in self._rulers_by_scope.values())
            self._rulers_by_scope.clear()
        self.changed.emit()
        self.window.view_state_changed.emit()
        self.window.canvas.viewport().update()
        if removed:
            self.window.status.setText(f'已清除当前视图 {removed} 条标尺。')
        else:
            self.window.status.setText('当前视图没有已放置的标尺。')

    def _constrain(self, start: tuple[int, int], end: tuple[int, int]) -> tuple[int, int]:
        if self.mode == 'diagonal':
            return end
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        return (end[0], start[1]) if abs(dx) >= abs(dy) else (start[0], end[1])

    def _snap_point(self, pixel_pos: QPointF, *, allow_edge_snap: bool = True) -> tuple[tuple[int, int], bool]:
        e = self.window.editor
        canvas = self.window.canvas
        pt = canvas.mapToScene(pixel_pos.toPoint())
        raw_x, raw_y = pt.x(), -pt.y()
        grid = e.profile.grid
        gx = round(raw_x / grid) * grid
        gy = round(raw_y / grid) * grid
        if not allow_edge_snap:
            return (gx, gy), False

        scale = max(abs(canvas.transform().m11()), 1e-9)
        tol_dbu = 8.0 / scale
        px, py = int(round(pixel_pos.x())), int(round(pixel_pos.y()))
        probe = QRect(px - 9, py - 9, 19, 19)
        xs: list[int] = []
        ys: list[int] = []
        hidden = getattr(self.window, 'hidden_layers', ())
        for item in canvas.items(probe):
            if not item.isVisible() or item.zValue() >= 100000:
                continue
            if isinstance(item, NativeFigureItem):
                if item.style['lpp'][0] in hidden:
                    continue
            elif not isinstance(item, PreviewRectItem):
                continue
            rect = item.sceneBoundingRect()
            if rect.isEmpty():
                continue
            xs.extend((round(rect.left() / grid) * grid, round(rect.right() / grid) * grid))
            ys.extend((round(-rect.bottom() / grid) * grid, round(-rect.top() / grid) * grid))

        local = e.w.local_data
        scope = e.scope_id
        if scope and scope in local.regions:
            r = local.regions[scope]
            for (bx0, by0), (bx1, by1) in [r.bbox, *(pin.bbox for pin in r.pins.values())]:
                xs.extend((bx0, bx1))
                ys.extend((by0, by1))

        best_x = min(xs, key=lambda v: abs(v - raw_x), default=None)
        best_y = min(ys, key=lambda v: abs(v - raw_y), default=None)
        snapped_x = best_x if best_x is not None and abs(best_x - raw_x) <= tol_dbu else gx
        snapped_y = best_y if best_y is not None and abs(best_y - raw_y) <= tol_dbu else gy
        snapped_edge = (snapped_x != gx) or (snapped_y != gy) or (
            best_x is not None and abs(best_x - raw_x) <= tol_dbu
        ) or (best_y is not None and abs(best_y - raw_y) <= tol_dbu)
        return (snapped_x, snapped_y), snapped_edge

    def eventFilter(self, obj, event):
        if not self.active:
            return False
        e = self.window.editor
        if not e or self.window.busy or self._current_key() != self._scope_key:
            self.cancel_capture()
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
            # Preserve middle-button panning during active ruler measurement.
            if (
                kind == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.MiddleButton
            ) or self.window.canvas._pan_last is not None:
                return False

            scene_pt = self.window.canvas.mapToScene(event.position().toPoint())
            self.window.canvas.cursor_moved.emit(scene_pt.x(), -scene_pt.y())
            if kind == QEvent.Type.MouseButtonRelease:
                return True

            allow_edge = not bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            point, snapped_edge = self._snap_point(event.position(), allow_edge_snap=allow_edge)
            self._snapped_edge = snapped_edge
            target = self._constrain(self.p0, point) if self.p0 is not None else point
            self.cursor = target

            if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                if event.button() == Qt.MouseButton.LeftButton:
                    if self.p0 is None:
                        self.p0 = point
                        self.cursor = point
                    elif target != self.p0:
                        seg = RulerSegment(self.p0, target, self.mode)
                        bucket = self._rulers_by_scope.setdefault(self._scope_key, [])
                        bucket.append(seg)
                        while len(bucket) > MAX_RULERS_PER_SCOPE:
                            bucket.pop(0)
                        dx = target[0] - self.p0[0]
                        dy = target[1] - self.p0[1]
                        dist = hypot(dx, dy)
                        scale = e.profile.dbu_per_micron
                        self.cancel_capture()
                        self.window.status.setText(
                            f'标尺：{format_um(dist, scale)} µm'
                            f'（ΔX={format_um(dx, scale)}, ΔY={format_um(dy, scale)} µm）· Shift+K 清除'
                        )
                        return True
                elif event.button() == Qt.MouseButton.RightButton:
                    if self.p0 is not None:
                        self.p0 = None
                    else:
                        self.cancel_capture()
                        return True

            self.changed.emit()
            self.window.canvas.viewport().update()
            return True

        if kind == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
                self.mode = 'diagonal' if self.mode == 'orthogonal' else 'orthogonal'
                if self.p0 is not None and self.cursor is not None:
                    self.cursor = self._constrain(self.p0, self.cursor)
                mode_label = '正交' if self.mode == 'orthogonal' else '斜向'
                self.window.status.setText(f'标尺模式已切换为：{mode_label}')
                self.changed.emit()
                self.window.canvas.viewport().update()
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.cancel_capture()
                self.window.status.setText('已取消当前标尺测距（已放置标尺保留；Shift+K 清除）。')
                return True
        return False

    def paint(self, painter):
        e = self.window.editor
        if not e:
            return
        rulers = self.current_rulers()
        has_live = self.active and self.cursor is not None
        if not rulers and not has_live:
            return

        canvas = self.window.canvas
        tr = canvas.viewportTransform()
        scale_px = max(abs(tr.m11()), 1e-9)
        dbu_scale = e.profile.dbu_per_micron
        tick_step = _nice_tick_step_dbu(scale_px, dbu_scale)

        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        painter.resetTransform()
        font = ui_font(pixel_size=11)
        painter.setFont(font)
        metrics = QFontMetricsF(font)

        def draw_segment(seg: RulerSegment, *, preview: bool):
            s0 = tr.map(QPointF(seg.p0[0], -seg.p0[1]))
            s1 = tr.map(QPointF(seg.p1[0], -seg.p1[1]))
            vx = s1.x() - s0.x()
            vy = s1.y() - s0.y()
            px_len = hypot(vx, vy)
            if px_len < 0.5:
                return
            ux, uy = vx / px_len, vy / px_len
            nx, ny = -uy, ux

            accent = QColor('#ffd97d' if preview else '#f6c85f')
            halo = QPen(QColor(17, 19, 23, 210), 3.2)
            line_pen = QPen(accent, 1.5, Qt.PenStyle.DashLine if preview else Qt.PenStyle.SolidLine)

            if seg.mode == 'diagonal' and seg.p0[0] != seg.p1[0] and seg.p0[1] != seg.p1[1]:
                corner = tr.map(QPointF(seg.p1[0], -seg.p0[1]))
                comp_pen = QPen(QColor(246, 200, 95, 135), 1.0, Qt.PenStyle.DotLine)
                painter.setPen(comp_pen)
                painter.drawLine(s0, corner)
                painter.drawLine(corner, s1)

            painter.setPen(halo)
            painter.drawLine(s0, s1)
            for pt in (s0, s1):
                painter.drawLine(
                    QPointF(pt.x() - nx * 6.0, pt.y() - ny * 6.0),
                    QPointF(pt.x() + nx * 6.0, pt.y() + ny * 6.0),
                )

            painter.setPen(line_pen)
            painter.drawLine(s0, s1)
            solid_pen = QPen(accent, 1.5)
            painter.setPen(solid_pen)
            for pt in (s0, s1):
                painter.drawLine(
                    QPointF(pt.x() - nx * 6.0, pt.y() - ny * 6.0),
                    QPointF(pt.x() + nx * 6.0, pt.y() + ny * 6.0),
                )

            world_len = hypot(seg.p1[0] - seg.p0[0], seg.p1[1] - seg.p0[1])
            if tick_step and px_len >= 54 and world_len > tick_step:
                count = min(64, int(world_len // tick_step))
                tick_pen = QPen(accent, 1.1)
                painter.setPen(tick_pen)
                for i in range(1, count + 1):
                    frac = (i * tick_step) / world_len
                    if frac >= 0.995:
                        break
                    tx = s0.x() + vx * frac
                    ty = s0.y() + vy * frac
                    half = 3.8 if i % 5 == 0 else 2.4
                    painter.drawLine(
                        QPointF(tx - nx * half, ty - ny * half),
                        QPointF(tx + nx * half, ty + ny * half),
                    )

            dx = seg.p1[0] - seg.p0[0]
            dy = seg.p1[1] - seg.p0[1]
            if seg.mode == 'diagonal' and dx != 0 and dy != 0:
                label = (
                    f'{format_um(world_len, dbu_scale)} µm'
                    f' ({format_um(abs(dx), dbu_scale)}×{format_um(abs(dy), dbu_scale)})'
                )
            else:
                label = f'{format_um(world_len, dbu_scale)} µm'

            tw = metrics.horizontalAdvance(label) + 10
            th = metrics.height() + 4
            mx = (s0.x() + s1.x()) * 0.5 + nx * 13.0
            my = (s0.y() + s1.y()) * 0.5 + ny * 13.0
            badge = QRectF(mx - tw * 0.5, my - th * 0.5, tw, th)
            painter.setPen(QPen(accent, 1.0))
            painter.setBrush(QColor(20, 23, 28, 224))
            painter.drawRoundedRect(badge, 3.0, 3.0)
            painter.setPen(QColor('#f5e6c4' if not preview else '#fff3cf'))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)

        for committed in rulers:
            draw_segment(committed, preview=False)

        if self.active and self.p0 is not None and self.cursor is not None and self.cursor != self.p0:
            draw_segment(RulerSegment(self.p0, self.cursor, self.mode), preview=True)

        if self.active and self.cursor is not None:
            cp = tr.map(QPointF(self.cursor[0], -self.cursor[1]))
            snap_color = QColor('#6be0b3' if self._snapped_edge else '#ffd97d')
            painter.setPen(QPen(QColor(17, 19, 23, 220), 3.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(QPointF(cp.x() - 6, cp.y()), QPointF(cp.x() + 6, cp.y()))
            painter.drawLine(QPointF(cp.x(), cp.y() - 6), QPointF(cp.x(), cp.y() + 6))
            painter.setPen(QPen(snap_color, 1.4))
            painter.drawLine(QPointF(cp.x() - 6, cp.y()), QPointF(cp.x() + 6, cp.y()))
            painter.drawLine(QPointF(cp.x(), cp.y() - 6), QPointF(cp.x(), cp.y() + 6))
            if self._snapped_edge:
                painter.drawRect(QRectF(cp.x() - 3.5, cp.y() - 3.5, 7.0, 7.0))

        painter.restore()
