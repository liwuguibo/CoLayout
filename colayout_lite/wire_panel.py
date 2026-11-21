"""Human wire creation with local, uncommitted geometry and RC previews."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .drawing_mode import cancel_drawing
from .geometry import via_figures
from .geometry_paths import box_rect
from .model import Shape
from .regions.query import metals
from .wire_capture import POSTURE_LABELS, DraftRoute, WireCapture
from .wire_defaults import default_width


class WirePanel(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.setMinimumWidth(290)
        self.window = window
        self.capture = WireCapture(
            window, self.preview, self.finish, step_layer=self.step_layer
        )
        self.capture.active_changed.connect(self.active_changed)
        self.destroyed.connect(lambda *args: self.capture.dispose())
        self.draft = None
        self._profile = None
        self._last_default_width: float | None = None
        self._last_route_uids: tuple[str, ...] = ()
        self._last_route_before = None
        self._last_route_after = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.layer = QComboBox()
        form.addRow('下一段金属', self.layer)
        self.width = QDoubleSpinBox()
        self.width.setDecimals(6)
        self.width.setRange(0.000001, 10000)
        self.width.setSuffix(' µm')
        form.addRow('线宽', self.width)
        self.via = QComboBox()
        form.addRow('换层标准过孔', self.via)
        self.rows = QSpinBox()
        self.rows.setRange(1, 64)
        self.columns = QSpinBox()
        self.columns.setRange(1, 64)
        via_counts = QHBoxLayout()
        via_counts.addWidget(self.rows)
        via_counts.addWidget(QLabel('行 ×'))
        via_counts.addWidget(self.columns)
        via_counts.addWidget(QLabel('列'))
        form.addRow('切孔阵列', via_counts)
        self.net = QLineEdit()
        self.net.setPlaceholderText('可选网络标记；不代替物理连通证据')
        form.addRow('网络标记', self.net)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.draw = QPushButton('画线')
        self.draw.setObjectName('canvasDraw')
        self.draw.setCheckable(True)
        self.draw.clicked.connect(self.start)
        self.finish_button = QPushButton('确认')
        self.finish_button.clicked.connect(self.capture.end)
        self.cancel_button = QPushButton('取消')
        self.cancel_button.clicked.connect(self.cancel_draw)
        row.addWidget(self.draw)
        row.addWidget(self.finish_button)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.inspect_rc_button = QPushButton('查看本次走线 RC')
        self.inspect_rc_button.setObjectName('wireInspectRc')
        self.inspect_rc_button.setToolTip('仅在走线完成或选中导线后，按需计算本次走线的 R / Cg / Cc。')
        self.inspect_rc_button.clicked.connect(self.inspect_route_rc)
        layout.addWidget(self.inspect_rc_button)
        self.message = QLabel(
            '单击加点（自动 L 形正交预览，Space 翻转折弯），[ / ] 切换下层/上层金属并自动匹配过孔；\n'
            '右键撤销上一步，Enter/双击确认，Esc 取消。提交不新增全区 DRC/连通门槛。'
        )
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        layout.addStretch(1)
        self.layer.currentIndexChanged.connect(self.layer_changed)
        self.width.valueChanged.connect(self.segment_changed)
        self.via.currentIndexChanged.connect(self.segment_changed)
        self.rows.valueChanged.connect(self.via_parameters_changed)
        self.columns.valueChanged.connect(self.via_parameters_changed)
        window.canvas.wire_overlay = self.paint
        window.view_state_changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        editor = self.window.editor
        if not editor:
            return
        if self._profile is not editor.profile:
            self._profile = editor.profile
            current = self.layer.currentData()
            self.layer.clear()
            for layer in metals(editor.profile):
                self.layer.addItem(layer, layer)
            self.layer.setCurrentIndex(max(0, self.layer.findData(current)))
            initial = default_width(editor.profile, self.layer.currentData())
            self._last_default_width = initial
            self.width.setValue(initial)
            self.layer_changed()
        enabled = (
            not self.window.busy
            and editor.w.state not in ('submitting', 'indeterminate', 'conflict')
            and not editor.work_sessions.readonly_reason()
        )
        self.draw.setEnabled(enabled)
        self.layer.setEnabled(enabled)
        self.width.setEnabled(enabled)
        self.via.setEnabled(enabled)
        self.finish_button.setEnabled(self.capture.active)
        self.cancel_button.setEnabled(self.capture.active)
        has_last_route = bool(self._last_route_uids and editor.w is self._last_route_after)
        human = getattr(self.window, 'human', None)
        has_selection = bool(human and human.ids())
        self.inspect_rc_button.setEnabled(
            bool(editor.profile.parasitics and not self.capture.active and (has_last_route or has_selection))
        )

    def layer_changed(self):
        if not self.window.editor:
            return
        profile = self.window.editor.profile
        selected = self.layer.currentData()
        target_default = default_width(profile, selected)
        if (
            self.width.value() <= 0
            or self._last_default_width is None
            or abs(self.width.value() - self._last_default_width) < 1e-9
        ):
            self.width.setValue(target_default)
        self._last_default_width = target_default

        current = self.via.currentData()
        prev_layer = self.capture.draft.layers[-1] if self.capture.draft.layers else None
        self.via.clear()
        preferred_index = -1
        for name, definition in profile.vias.items():
            if definition.get('kind') != 'stdViaDef' or definition.get('cut_pattern'):
                continue
            if selected in (definition.get('layer1'), definition.get('layer2')):
                other = (
                    definition['layer2']
                    if selected == definition['layer1']
                    else definition['layer1']
                )
                self.via.addItem(f'{selected} ↔ {other} · {name}', name)
                if prev_layer and other == prev_layer and preferred_index < 0:
                    preferred_index = self.via.count() - 1
        if preferred_index >= 0:
            self.via.setCurrentIndex(preferred_index)
        else:
            self.via.setCurrentIndex(max(0, self.via.findData(current)))
        self.segment_changed()

    def step_layer(self, delta: int):
        count = self.layer.count()
        if count <= 0:
            return
        target = max(0, min(count - 1, self.layer.currentIndex() + delta))
        if target == self.layer.currentIndex():
            return
        self.layer.setCurrentIndex(target)
        layer_name = self.layer.currentData()
        via_name = self.via.currentData()
        if hasattr(self.window, 'status'):
            extra = f' · 换层过孔 {via_name}' if via_name else ''
            self.window.status.setText(
                f'下一段金属：{layer_name}（默认线宽 {self.width.value():g} µm{extra} · {POSTURE_LABELS[self.capture.posture]}）'
            )
        self.window.canvas.viewport().update()

    def segment_changed(self):
        self.capture.set_segment(
            self.layer.currentData(), self.width.value(), self.via.currentData()
        )

    def via_parameters_changed(self):
        self.capture.set_via_parameters(self.rows.value(), self.columns.value())

    def start(self, checked):
        if not checked:
            self.cancel_draw()
            return
        window = self.window
        cancel_drawing(window, self)
        window.selection_ui.set_ids([])
        self.segment_changed()
        self.capture.start(
            layer=self.layer.currentData(),
            width_um=self.width.value(),
            via_definition=self.via.currentData(),
            cut_rows=self.rows.value(),
            cut_columns=self.columns.value(),
            net_intent=self.net.text().strip(),
        )
        self.draw.setChecked(True)
        if hasattr(window, 'wire_popup'):
            window.wire_popup.dismiss_for_drawing()
        self.refresh()
        window.view_state_changed.emit()

    def active_changed(self, active):
        self.draw.setChecked(active)
        self.refresh()
        self.window.view_state_changed.emit()

    def preview(self, draft):
        self.draft = draft
        if draft is not None and self.window.editor:
            try:
                draft.shapes(self.window.editor.profile)
            except Exception as exc:
                self.message.setText('走线草案提示：' + str(exc) + '；仍可继续画线。')
        self.window.canvas.viewport().update()

    def cancel_draw(self, *_):
        was_active = self.capture.active
        self.capture.cancel()
        self.draft = None
        self.refresh()
        if not was_active:
            self.window.view_state_changed.emit()

    def finish(self, draft):
        window = self.window
        editor = window.editor
        was_active = self.capture.active
        if was_active:
            self.capture.active = False
            self.capture.draft = DraftRoute()
            self.capture._history.clear()
            self.capture.cursor = None
            window.canvas.unsetCursor()
        self.draft = None
        before_ws = editor.w
        try:
            shapes = draft.shapes(editor.profile)
            created = []
            with editor.transaction() as staged:
                for shape in shapes:
                    created.append(staged.create(shape))
            self._last_route_uids = tuple(created)
            self._last_route_before = before_ws if created else None
            self._last_route_after = editor.w if created else None
            window.selection_ui.set_ids(created)
            msg = f'已创建 {len(created)} 个图形；可点击“查看本次走线 RC”估算寄生，Ctrl+Z 可撤销。'
            self.message.setText(msg)
            if hasattr(window, 'status'):
                window.status.setText(msg)
        except Exception as exc:
            self.message.setText(str(exc))
            if hasattr(window, 'status'):
                window.status.setText(str(exc))
        if was_active:
            self.capture.active_changed.emit(False)
        else:
            self.refresh()
            window.view_state_changed.emit()

    def inspect_route_rc(self):
        editor = self.window.editor
        if not editor or not editor.profile.parasitics:
            self.message.setText('请先在工艺库配置并审核 RC 参数。')
            return
        target_uids = None
        baseline = None
        baseline_uids = None
        if self._last_route_uids and editor.w is self._last_route_after:
            target_uids = self._last_route_uids
            baseline = self._last_route_before
            baseline_uids = target_uids
        else:
            human = getattr(self.window, 'human', None)
            ids = tuple(sorted(human.ids())) if human else ()
            if ids:
                target_uids = ids
        self.window.parasitics.show()
        self.window.parasitics.evaluate_now(
            target_uids,
            baseline_workspace=baseline,
            baseline_target_uids=baseline_uids,
        )

    def paint(self, painter):
        if not self.capture.active:
            return
        editor = self.window.editor
        if not editor:
            return
        profile = editor.profile
        painter.save()
        painter.setTransform(self.window.canvas.viewportTransform())
        if self.draft:
            try:
                geometry = self.draft.geometry(profile)
            except Exception:
                geometry = None
            if geometry is not None:
                from .inspection_view import polygon_path

                for row in geometry.conductors:
                    color = QColor(
                        *self.window.styles.get(row.layer + '/drawing', {}).get(
                            'fill_rgb', [105, 219, 149]
                        )
                    )
                    pen = QPen(color, 1.5)
                    pen.setCosmetic(True)
                    painter.setPen(pen)
                    color.setAlpha(100)
                    painter.setBrush(color)
                    painter.drawPath(polygon_path(row.geometry))

        # Pending layer-transition via enclosure preview at the current turn anchor
        committed = self.capture.draft
        if (
            committed.points
            and committed.layers
            and self.capture.layer
            and self.capture.layer != committed.layers[-1]
            and self.capture.via_definition in profile.vias
        ):
            try:
                via_shape = Shape(
                    kind='stdVia',
                    origin=committed.points[-1],
                    via_definition=self.capture.via_definition,
                    via_parameters={
                        'cutRows': self.rows.value(),
                        'cutColumns': self.columns.value(),
                    },
                )
                for fig in via_figures(via_shape, profile):
                    layer_name = fig['lpp'][0]
                    rgb = self.window.styles.get(layer_name + '/drawing', {}).get(
                        'outline_rgb', [255, 214, 102]
                    )
                    pen = QPen(QColor(*rgb), 1.4, Qt.PenStyle.DashLine)
                    pen.setCosmetic(True)
                    fill = QColor(*rgb)
                    fill.setAlpha(55)
                    painter.setPen(pen)
                    painter.setBrush(fill)
                    painter.drawRect(box_rect(fig['bbox']))
            except Exception:
                pass
        painter.restore()

    def closeEvent(self, event):
        self.capture.dispose()
        super().closeEvent(event)


def show(window):
    if not hasattr(window, 'wire_panel'):
        panel = WirePanel(window)
        if getattr(getattr(window, 'studio', None), 'mode', None) == 'studio':
            from .tool_popover import ToolPopover

            popup = ToolPopover(window, '画线', panel, panel.cancel_draw)
            window.wire_popup = popup
            window.wire_panel = panel
            window.wire_dock = popup
        else:
            dock = QDockWidget('画线', window)
            dock.setObjectName('wireDock')
            dock.setWidget(panel)
            window.wire_panel = panel
            window.wire_dock = dock
            window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            from .panel_chrome import style_dock

            style_dock(window, dock)
            dock.visibilityChanged.connect(
                lambda visible: panel.refresh() if visible else panel.cancel_draw()
            )
    if hasattr(window, 'wire_popup'):
        button = window.studio.action_buttons['wire'][0]
        if window.wire_popup.isVisible():
            window.wire_popup.hide()
        else:
            window.wire_popup.show_near(button)
            window.view_state_changed.emit()
    else:
        window.wire_dock.show()
        window.wire_dock.raise_()
    return window.wire_panel
