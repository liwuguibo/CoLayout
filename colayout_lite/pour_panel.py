"""Canvas metal-pour creation through the shared layout-helper service."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDockWidget, QDoubleSpinBox, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .drawing_mode import cancel_drawing
from .layer_choices import populate_layers
from .model import LiteError
from .regions.actions import RegionActions


class PourPanel(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.setMinimumWidth(280)
        self.window = window
        self.context = None
        self._profile = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.layers = QComboBox()
        form.addRow('金属层', self.layers)
        self.net = QLineEdit()
        self.net.setPlaceholderText('例如 VSS')
        form.addRow('目标网络', self.net)
        self.clearance = QDoubleSpinBox()
        self.clearance.setDecimals(6)
        self.clearance.setRange(0.000001, 1000000)
        self.clearance.setSuffix(' µm')
        form.addRow('最小间距', self.clearance)
        layout.addLayout(form)
        self.targets = QLabel('连接目标：未选择；将保留所有独立铜岛。')
        self.targets.setWordWrap(True)
        layout.addWidget(self.targets)
        self.draw = QPushButton('拖框铺铜')
        self.draw.setObjectName('canvasDraw')
        self.draw.setCheckable(True)
        self.draw.clicked.connect(self.start)
        layout.addWidget(self.draw)
        self.message = QLabel('可先选择要连接的同层图形，再拖出铺铜范围；其他金属按间距避让。Esc 取消。')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        layout.addStretch()
        window.canvas.region_picked.connect(self.picked)
        window.canvas.region_cancelled.connect(self.cancel_draw)
        window.view_state_changed.connect(self.refresh)
        self.layers.currentIndexChanged.connect(self.update_clearance)
        self.refresh()

    def update_clearance(self, *_):
        self.cancel_draw()
        rules = getattr(getattr(self.window, 'drc', None), 'profile', None)
        layer = self.layers.currentData()[0] if self.layers.currentData() else None
        values = [
            rule.value
            for rule in getattr(rules, 'rules', ())
            if rule.enabled and rule.kind == 'spacing' and rule.layer == layer
        ]
        self.clearance.setValue(max(values, default=0.2))

    def refresh(self):
        window = self.window
        editor = window.editor
        if not editor:
            return
        if self.context and (
            editor is not self.context[0]
            or editor.w is not self.context[1]
            or editor.scope_id != self.context[2]
        ):
            self.cancel_draw()
        if self._profile is not editor.profile:
            old = self.layers.currentData()
            self._profile = editor.profile
            populate_layers(self.layers, editor.profile, old, metal_only=True)
            self.update_clearance()
        if not self.context:
            ids = window.human.ids()
            self.targets.setText(
                f'连接目标：已选择 {len(ids)} 个对象；只接受当前层普通图形/过孔。'
                if ids
                else '连接目标：未选择；将保留所有独立铜岛。'
            )
        enabled = bool(
            not window.busy
            and editor.w.state not in ('submitting', 'indeterminate', 'conflict')
            and not editor.work_sessions.readonly_reason()
            and self.layers.count()
        )
        self.draw.setEnabled(enabled)
        self.layers.setEnabled(enabled)
        self.net.setEnabled(enabled)
        self.clearance.setEnabled(enabled)

    def start(self, on):
        if not on:
            self.cancel_draw()
            return
        window = self.window
        if not self.net.text().strip():
            self.draw.setChecked(False)
            self.message.setText('请先填写目标网络。')
            window.status.setText(self.message.text())
            return
        cancel_drawing(window, self)
        target_ids = sorted(window.human.ids())
        self.context = (
            window.editor,
            window.editor.w,
            window.editor.scope_id,
            self.layers.currentData(),
            self.net.text().strip(),
            target_ids,
        )
        window.selection_ui.set_ids([])
        self.draw.setChecked(True)
        window.canvas.region_select = True
        window.canvas.setCursor(Qt.CursorShape.CrossCursor)
        window.canvas.setFocus()
        if hasattr(window, 'pour_popup'):
            window.pour_popup.dismiss_for_drawing()
        self.message.setText('在画布拖出铺铜范围，松手生成；Esc 取消。')

    def cancel_draw(self, *_):
        if self.context:
            self.window.canvas.region_select = False
            self.window.canvas.unsetCursor()
        self.context = None
        self.draw.setChecked(False)

    def picked(self, bounds):
        if self.context is None:
            return
        editor, source, scope, lpp, net, target_ids = self.context
        window = self.window
        self.cancel_draw()
        try:
            if editor is not window.editor or editor.w is not source or editor.scope_id != scope:
                raise LiteError('DRAW_CONTEXT_CHANGED', '工作区已变化，请重新拖框铺铜。')
            editor.work_sessions.assert_editable()
            (x, y), (xx, yy) = RegionActions(editor).snap_box(bounds)
            scale = editor.profile.dbu_per_micron
            from .layout_helpers.service import execute

            result = execute(
                editor,
                'create_pour',
                {
                    'bounds_um': [x / scale, y / scale, xx / scale, yy / scale],
                    'lpp': lpp,
                    'net_intent': net,
                    'clearance': self.clearance.value(),
                    'targets': target_ids or None,
                },
                rules=getattr(getattr(window, 'drc', None), 'profile', None),
            )
            window.selection_ui.set_ids(result['created_uids'])
            self.message.setText(
                f"已生成 {result['polygon_count']} 个多边形，面积 {result['area_um2']:.6g} µm²；"
                f"移除 {result['discarded_island_count']} 个未连接铜岛。Ctrl+Z 可一次撤销；请运行 DRC。"
            )
        except Exception as exc:
            self.message.setText(str(exc))
        window.status.setText(self.message.text())


def show(window):
    if not hasattr(window, 'pour_panel'):
        panel = PourPanel(window)
        if getattr(getattr(window, 'studio', None), 'mode', None) == 'studio':
            from .tool_popover import ToolPopover

            popup = ToolPopover(window, '铺铜', panel, panel.cancel_draw)
            window.pour_popup = popup
            window.pour_panel = panel
            window.pour_dock = popup
        else:
            dock = QDockWidget('铺铜', window)
            dock.setObjectName('pourDock')
            dock.setWidget(panel)
            window.pour_panel = panel
            window.pour_dock = dock
            window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            from .panel_chrome import style_dock

            style_dock(window, dock)
            dock.visibilityChanged.connect(lambda visible: panel.refresh() if visible else panel.cancel_draw())
    if hasattr(window, 'pour_popup'):
        button = window.studio.action_buttons['pour'][0]
        window.pour_popup.show_near(button)
    else:
        window.pour_dock.show()
        window.pour_dock.raise_()
