"""Canvas-first presentation over the shared editor. Panels open only on request.

The production canvas-first workspace shell.
All panels share one canvas, editor and history.
"""

from __future__ import annotations

from math import hypot

from PySide6.QtCore import QObject, QSignalBlocker, QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFrame,
    QGraphicsRectItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QSplitter,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .camera import capture, restore
from .compact_style import ICON_COLOR, apply_window_theme
from .layer_palette import StudioLayerPalette
from .ruler_tool import format_um
from .studio_commands import StudioCommands
from .studio_icons import icon
from .studio_panels import StudioHierarchy, StudioInspector
from .theme import palette, set_visual_theme
from .ui_signals import ElidedLabel


class StudioShell(QObject):
    def __init__(self, window, *, mode=None):
        super().__init__(window)
        self.window = window
        self.mode = None
        self._refreshing = False
        self._display_key = None
        self._crumb_key = None
        self._theme_snapshot = None
        self._last_cursor_dbu: tuple[float, float] | None = None
        self.classic = window.takeCentralWidget()
        self.classic.hide()
        self.host = QWidget()
        layout = QVBoxLayout(self.host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.formal = QWidget()
        self.formal.setObjectName('compactChrome')
        layout.addWidget(self.formal)
        window.setCentralWidget(self.host)
        self.commands = StudioCommands(window)
        self.action_buttons = {}
        self.menus = {}
        self.panel_state = {'hierarchy': False, 'layers': False, 'inspector': False}
        outer = QVBoxLayout(self.formal)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.build_toolbar()
        self.build_body(outer)
        self.build_footer(outer)
        from .sync_indicator import SyncIndicator

        self.sync_indicator = SyncIndicator(
            self.commands.actions['sync'], ICON_COLOR, self
        )
        self.refresh_indicator = SyncIndicator(
            self.commands.actions['refresh'], ICON_COLOR, self, idle_icon='import'
        )
        window.status.text_changed.connect(self.status.setText)
        self.status.setText(window.status.text())
        window.view_state_changed.connect(self.refresh)
        window.canvas.region_picked.connect(
            lambda _: window.status.setText(window.region_panel.message.text())
        )
        window.canvas.cursor_moved.connect(self.update_cursor_coords)
        window.canvas.cursor_left.connect(self.clear_cursor_coords)
        window.ruler.changed.connect(self._refresh_coords_from_last)
        # A launch starts with a full canvas, even when upgrading ribbon prefs.
        # Explicit mode switches within this session retain panel visibility.
        self.setup_presentation()

    def menu_button(self, title, key):
        button = QToolButton()
        button.setText(title)
        button.setObjectName('compactMenuButton')
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(button)
        menu.setObjectName('compactMenu_' + key)
        button.setMenu(menu)
        self.toolbar.addWidget(button)
        self.menus[key] = menu
        return menu

    def add_actions(self, menu, keys):
        for key in keys:
            if key is None:
                menu.addSeparator()
            else:
                menu.addAction(self.commands.actions[key])

    def toolbar_action(self, key, *, icon_only=False):
        action = self.commands.actions[key]
        self.toolbar.addAction(action)
        button = self.toolbar.widgetForAction(action)
        button.setObjectName('compactButton_' + key)
        if icon_only:
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.action_buttons.setdefault(key, []).append(button)
        return button

    def check_action(self, menu, label, source):
        action = QAction(label, self)
        action.setCheckable(True)
        action.setChecked(source.isChecked())
        action.toggled.connect(source.setChecked)
        source.toggled.connect(action.setChecked)
        menu.addAction(action)
        return action

    def rectangle_menu(self):
        menu = self.menu_button('矩形', 'rectangle')
        self.add_actions(menu, ('rectangle', 'pour'))
        button = menu.parentWidget()
        for key in ('rectangle', 'pour'):
            self.action_buttons.setdefault(key, []).append(button)
        return menu

    def build_toolbar(self):
        self.toolbar = QToolBar()
        self.toolbar.setObjectName('compactToolbar')
        self.toolbar.setMovable(False)
        self.toolbar.setIconSize(QSize(16, 16))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        # Let Qt expand overflow rows on narrow/high-DPI displays.
        self.toolbar.setMinimumHeight(40)
        self.toolbar.setMinimumWidth(0)
        brand = QLabel('CoLayout')
        brand.setObjectName('compactBrand')
        brand.setContentsMargins(10, 0, 10, 0)
        self.toolbar.addWidget(brand)
        self.mode_badge = QLabel('离线试用')
        self.mode_badge.setObjectName('compactSyncBadge')
        self.mode_badge.setToolTip(
            '此窗口不使用远端通信。请通过 CoLayout 工作区启动器打开正式工作区。'
        )
        self.mode_badge_action = self.toolbar.addWidget(self.mode_badge)
        self.mode_badge_action.setVisible(not getattr(self.window, 'remote_enabled', True))

        file = self.menu_button('文件', 'file')
        self.add_actions(
            file,
            (
                'open',
                'save',
                'save_as',
                'reference_cases',
                None,
                'import',
                'refresh',
                'sync',
                'native',
                'recover',
                'fork',
                None,
                'process',
                'shortcuts',
            ),
        )
        edit = self.menu_button('编辑', 'edit')
        self.add_actions(
            edit,
            (
                'undo',
                'redo',
                None,
                'rotate',
                'mirror_h',
                'mirror_v',
                None,
                'copy_id',
                'copy_task_context',
                'delete',
                'dissolve',
            ),
        )
        edit.addSeparator()
        self.check_action(edit, '允许拖动金属线和过孔', self.window.selection_ui.move_box)

        view = self.menu_button('视图', 'view')
        self.check_action(view, '显示实例内部', self.window.internal)
        self.check_action(view, '显示过孔', self.window.cuts)
        self.check_action(view, '原生纹理', self.window.pattern)
        self.check_action(view, '选择对象时 DIM', self.window.selection_ui.dim_box)
        peek = view.addMenu('模块透视')
        self.peek_group = QActionGroup(self)
        self.peek_group.setExclusive(True)
        self.peek_actions = []
        for i, label in enumerate(('不透明', '半透明 · 只展开一层', '透明 · 只展开一层')):
            action = peek.addAction(label)
            action.setCheckable(True)
            self.peek_group.addAction(action)
            action.triggered.connect(lambda checked=False, index=i: self.set_peek(index))
            self.peek_actions.append(action)

        check = self.menu_button('检查', 'check')
        self.add_actions(
            check,
            (
                'region_drc_local',
                'region_drc_remote',
                None,
                'ruler',
                'clear_rulers',
                'clear_net',
                None,
                'circuit',
                'activity',
            ),
        )
        self.toolbar.addSeparator()
        for key in ('region', 'markup', 'keepout'):
            self.toolbar_action(key)
        self.toolbar_action('wire')
        self.rectangle_menu()
        self.toolbar_action('guard')
        self.toolbar_action('ruler')
        self.toolbar.addSeparator()
        for key in ('undo', 'redo'):
            self.toolbar_action(key, icon_only=True)
        self.toolbar_action('copy_id')
        self.toolbar.addSeparator()

        self.panel_actions = {}
        view.addSeparator()
        for key, title, glyph in (
            ('hierarchy', '层次', 'hierarchy'),
            ('layers', '图层', 'layers'),
            ('inspector', '属性', 'panels'),
        ):
            action = QAction(icon(glyph, ICON_COLOR), title, self)
            action.setCheckable(True)
            action.setObjectName('compactPanel_' + key)
            action.toggled.connect(lambda checked, k=key: self.set_panel_visible(k, checked))
            self.panel_actions[key] = action
            self.toolbar.addAction(action)
            view.addAction(action)

        view.addSeparator()
        for key in ('ruler', 'clear_rulers'):
            view.addAction(self.commands.actions[key])
        view.addSeparator()
        self.toolbar_action('agent_api')
        for key in ('agent_api', 'autoroute', 'parasitics'):
            view.addAction(self.commands.actions[key])

        self.toolbar_action('fit', icon_only=True)
        self.document = ElidedLabel()
        self.document.setObjectName('compactSubtitle')
        self.document.setContentsMargins(12, 0, 8, 0)
        self.document.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.document.setMinimumWidth(0)
        self.toolbar.addWidget(self.document)
        self.sync_badge = QLabel()
        self.sync_badge.setObjectName('compactSyncBadge')
        self.toolbar.addWidget(self.sync_badge)
        self.toolbar_action('refresh')
        self.toolbar_action('sync')
        # Global commands span the window, including the docked tool area.
        # Otherwise opening a sidebar hides sync/refresh in toolbar overflow.
        self.window.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)

    def panel(self, key, title, content):
        wrapper = QWidget()
        wrapper.setObjectName('compactPanel')
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        from .panel_chrome import header

        layout.addWidget(header(title, lambda: self.set_panel_visible(key, False)))
        layout.addWidget(content, 1)
        return wrapper

    def build_body(self, layout):
        self.body = QSplitter()
        self.body.setHandleWidth(1)
        self.body.setChildrenCollapsible(False)
        self.hierarchy = StudioHierarchy(self.window)
        self.layers = StudioLayerPalette(self.window)
        self.inspector = StudioInspector(self.window)
        self.hierarchy.inspected.connect(self.inspect_from_tree)
        inspector = QDockWidget('属性', self.window)
        inspector.setObjectName('studioInspectorDock')
        inspector.setWidget(self.inspector)
        inspector.setProperty('studioTool', True)
        self.window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, inspector)
        from .panel_chrome import style_dock

        style_dock(self.window, inspector, True)
        self.panels = {
            'hierarchy': self.panel('hierarchy', '层次', self.hierarchy),
            'layers': self.panel('layers', '图层', self.layers),
            'inspector': inspector,
        }
        inspector.visibilityChanged.connect(
            lambda _: self.inspector_closed() if inspector.isHidden() else None
        )
        self.left_column = QSplitter(Qt.Orientation.Vertical)
        self.left_column.setHandleWidth(1)
        self.left_column.setChildrenCollapsible(False)
        self.left_column.setMinimumWidth(210)
        self.left_column.setMaximumWidth(350)
        self.left_column.addWidget(self.panels['hierarchy'])
        self.left_column.addWidget(self.panels['layers'])
        self.panels['inspector'].setMinimumWidth(310)
        self.body.addWidget(self.left_column)
        self.center = QWidget()
        self.canvas_slot = QVBoxLayout(self.center)
        self.canvas_slot.setContentsMargins(0, 0, 0, 0)
        self.canvas_slot.setSpacing(0)
        self.body.addWidget(self.center)
        self.body.setStretchFactor(1, 1)
        self.body.setSizes([236, 1000])
        layout.addWidget(self.body, 1)
        for p in self.panels.values():
            p.hide()
        self.left_column.hide()

    def build_footer(self, layout):
        footer = QWidget()
        footer.setObjectName('compactFooter')
        footer.setFixedHeight(29)
        row = QHBoxLayout(footer)
        row.setContentsMargins(5, 0, 10, 0)
        row.setSpacing(8)
        up = QToolButton()
        up.setDefaultAction(self.commands.actions['up'])
        up.setObjectName('compactIconButton')
        up.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        row.addWidget(up)
        self.location = QToolButton()
        self.location.setObjectName('compactMenuButton')
        self.location.setMaximumWidth(260)
        self.location.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.location_menu = QMenu(self.location)
        self.location.setMenu(self.location_menu)
        row.addWidget(self.location)
        self.status = ElidedLabel()
        self.status.setObjectName('compactSubtitle')
        row.addWidget(self.status, 1)
        details = QToolButton()
        details.setText('详情')
        details.setToolTip('查看并复制完整操作记录')
        details.clicked.connect(self.window.activity.show)
        row.addWidget(details)
        self.summary = QLabel()
        self.summary.setObjectName('compactSelection')
        row.addWidget(self.summary)

        self.conductor_toggle = QToolButton()
        self.conductor_toggle.setObjectName('compactConductorToggle')
        self.conductor_toggle.setCheckable(True)
        move_box = self.window.selection_ui.move_box
        self.conductor_toggle.toggled.connect(move_box.setChecked)
        move_box.toggled.connect(self._sync_conductor_toggle)
        self._sync_conductor_toggle(move_box.isChecked())
        row.addWidget(self.conductor_toggle)

        self.selection_mode = QComboBox()
        self.selection_mode.addItems(['自动（左包含/右相交）', '完整包含', '相交即选'])
        self.selection_mode.setToolTip('框选方式')
        self.selection_mode.setMaximumWidth(162)
        self.selection_mode.setMaximumHeight(25)
        original = self.window.selection_ui.mode_combo
        self.selection_mode.setCurrentIndex(original.currentIndex())
        self.selection_mode.currentIndexChanged.connect(original.setCurrentIndex)
        original.currentIndexChanged.connect(self.selection_mode.setCurrentIndex)
        row.addWidget(self.selection_mode)

        self.coords = QLabel('X —  Y —')
        self.coords.setObjectName('compactCoords')
        self.coords.setMinimumWidth(148)
        self.coords.setToolTip('网格吸附实时坐标（活跃手势期间显示相对位移 ΔX, ΔY 与长度 L）')
        row.addWidget(self.coords)

        self.grid = QLabel()
        self.grid.setObjectName('compactSubtitle')
        self.grid.setToolTip('布局网格')
        row.addWidget(self.grid)
        layout.addWidget(footer)

    def _sync_conductor_toggle(self, movable: bool):
        with QSignalBlocker(self.conductor_toggle):
            self.conductor_toggle.setChecked(movable)
        if movable:
            self.conductor_toggle.setText('移线')
            self.conductor_toggle.setToolTip(
                '当前：允许直接拖动金属线与过孔（点击切换回“锁线”防误触查网模式）'
            )
        else:
            self.conductor_toggle.setText('锁线')
            self.conductor_toggle.setToolTip(
                '当前：锁定金属线与过孔拖动，左键单击直接高亮查看网络（点击切换为“移线”可拖动导线）'
            )

    def _refresh_coords_from_last(self):
        if self._last_cursor_dbu is not None:
            self.update_cursor_coords(*self._last_cursor_dbu)

    def clear_cursor_coords(self):
        self._last_cursor_dbu = None
        self.coords.setText('X —  Y —')

    def update_cursor_coords(self, x_dbu: float, y_dbu: float):
        self._last_cursor_dbu = (x_dbu, y_dbu)
        e = self.window.editor
        if not e:
            self.coords.setText('X —  Y —')
            return
        scale = e.profile.dbu_per_micron
        grid = e.profile.grid
        sx = round(x_dbu / grid) * grid
        sy = round(y_dbu / grid) * grid
        base = f'X {format_um(sx, scale)}  Y {format_um(sy, scale)}'

        extra = ''
        wire_panel = getattr(self.window, 'wire_panel', None)
        ruler = getattr(self.window, 'ruler', None)
        box_cap = getattr(self.window.canvas, 'box_capture', None)
        human = getattr(self.window, 'human', None)
        if wire_panel is not None and wire_panel.capture.active:
            delta = wire_panel.capture.active_delta_dbu()
            if delta is not None:
                dx, dy, seg_len = delta
                extra = (
                    f' · Δ({format_um(dx, scale)}, {format_um(dy, scale)})'
                    f' L={format_um(seg_len, scale)}'
                )
        elif ruler is not None and ruler.active:
            delta = ruler.active_delta_dbu()
            if delta is not None:
                dx, dy = delta
                extra = (
                    f' · Δ({format_um(dx, scale)}, {format_um(dy, scale)})'
                    f' L={format_um(hypot(dx, dy), scale)}'
                )
        elif box_cap is not None and box_cap.active and box_cap.start is not None and box_cap.end is not None:
            x0 = round(box_cap.start.x() / grid) * grid
            y0 = round(-box_cap.start.y() / grid) * grid
            w_dbu = abs(sx - x0)
            h_dbu = abs(sy - y0)
            extra = f' · {format_um(w_dbu, scale)}×{format_um(h_dbu, scale)}'
        elif human is not None and human.dragging and human.armed and 'delta' in human.armed:
            dx, dy = human.armed['delta']
            extra = f' · Δ({format_um(dx, scale)}, {format_um(dy, scale)})'

        self.coords.setText(base + extra + ' µm')

    def set_panel_visible(self, key, visible):
        if self.panel_state[key] == visible:
            if visible and key == 'inspector':
                self.panels[key].raise_()
            return
        state = capture(self.window.canvas)
        was_left = not self.left_column.isHidden()
        self.panel_state[key] = visible
        if key == 'inspector':
            if visible:
                from .tool_panels import show_tool

                show_tool(self.window, self.panels[key])
            else:
                self.panels[key].setVisible(False)
        else:
            self.panels[key].setVisible(visible)
            any_left = self.panel_state['hierarchy'] or self.panel_state['layers']
            self.left_column.setVisible(any_left)
            if any_left and not was_left:
                sizes = self.body.sizes()
                sizes[0] = 236
                self.body.setSizes(sizes)
            if self.panel_state['hierarchy'] and self.panel_state['layers']:
                self.left_column.setSizes([250, 320])
        with QSignalBlocker(self.panel_actions[key]):
            self.panel_actions[key].setChecked(visible)
        self.host.layout().activate()
        self.formal.layout().activate()
        self.body.refresh()
        self.canvas_slot.activate()
        restore(self.window.canvas, state)
        self.refresh()

    def inspector_closed(self):
        self.panel_state['inspector'] = False
        with QSignalBlocker(self.panel_actions['inspector']):
            self.panel_actions['inspector'].setChecked(False)

    def show_properties(self):
        self.set_panel_visible('inspector', True)
        self.inspector.refresh_selection()

    def inspect_from_tree(self, uid):
        # Tree inspection does not summon another pane. Properties can reveal
        # this row later without unexpectedly changing the camera or selection.
        self.inspector.inspect_uid(uid)

    def set_peek(self, index):
        canvas = self.window.canvas
        if index != canvas.blackbox_peek:
            self.window.human.cancel()
            canvas.blackbox_peek = index
            canvas.peek_changed.emit(index)

    def setup_presentation(self):
        w = self.window
        self.mode = 'studio'
        state = capture(w.canvas)
        for dock in (w.region_dock, w.workspace_dock):
            w.removeDockWidget(dock)
            dock.hide()
        w.drc_dock.hide()
        w.parasitics.dock.hide()
        from .panel_chrome import style_dock

        for dock in w.findChildren(QDockWidget):
            style_dock(w, dock, True)
        w.drc.set_presentation(True)
        w.statusBar().hide()
        self.canvas_slot.addWidget(w.canvas)
        self._theme_snapshot = apply_window_theme(w)
        set_visual_theme(w.canvas, 'compact')
        w.canvas.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        from .native_chrome import dark_titlebar

        w.setProperty('compactNativeTitlebar', dark_titlebar(w, True))
        w.canvas.setFrameShape(QFrame.Shape.NoFrame)
        for action in self.commands.actions.values():
            action.setIcon(icon(action.property('iconName'), ICON_COLOR))
        colors = palette(w.canvas)
        changed = set()
        from .module_chrome import native_frame_pen

        for uid, group in w.groups.items():
            for item in group.childItems():
                if isinstance(item, QGraphicsRectItem) and item.data(1) == 'interface':
                    item.setPen(native_frame_pen(colors))
                    item.setBrush(QBrush(QColor(colors.native_fill)))
                    changed.add(uid)
        if changed:
            w.canvas.invalidate_preview(changed)
        w.refresh_workspace_overlay()
        w.layout().activate()
        self.host.layout().activate()
        self.formal.layout().activate()
        self.body.refresh()
        self.canvas_slot.activate()
        restore(w.canvas, state)
        self.refresh()

    def refresh(self):
        if self._refreshing or not self.window.editor:
            return
        self._refreshing = True
        try:
            w = self.window
            e = w.editor
            self.commands.refresh()
            active = bool(w.busy and getattr(w, 'communication_active', False))
            action = getattr(w, 'communication_action', None) or 'sync'
            self.sync_indicator.set_active(active and action == 'sync')
            self.refresh_indicator.set_active(active and action == 'refresh')
            self.mode_badge_action.setVisible(not getattr(w, 'remote_enabled', True))
            for i, action in enumerate(self.peek_actions):
                with QSignalBlocker(action):
                    action.setChecked(w.canvas.blackbox_peek == i)
            self.hierarchy.refresh()
            self.layers.refresh()
            self.inspector.refresh()
            key = (
                id(e.w),
                e.scope_id,
                tuple((s.id, s.pose_revision) for s in e.work_sessions.snapshots()),
            )
            if key != self._display_key:
                self._display_key = key
                self.document.setText(e.w.baseline.library + ' / ' + e.w.baseline.cell)
                self.document.setToolTip(e.w.baseline.library + '/' + e.w.baseline.cell)
                count = len(e.w.operations)
                self.sync_badge.setText(
                    '待核对'
                    if e.w.state in ('submitting', 'indeterminate', 'conflict')
                    else f'{count} 项待同步'
                    if count
                    else ''
                )
                self.sync_badge.setToolTip(
                    '本地待同步变更；不表示实时服务器连接状态。'
                )
                self.refresh_breadcrumb()
            ids = w.human.ids()
            active = len(e.work_sessions.snapshots())
            self.summary.setText(
                (f'{active} 区域工作中  ·  ' if active else '')
                + (f'已选 {len(ids)} 项' if ids else '')
            )
            self.grid.setText(f'{e.profile.grid / e.profile.dbu_per_micron:g} µm')
        finally:
            self._refreshing = False

    def refresh_breadcrumb(self):
        e = self.window.editor
        trail = []
        scope = e.scope_id
        while scope:
            region = e.w.local_data.regions[scope]
            trail.append((scope, region.name))
            scope = region.parent_id
        trail = [(None, '顶层'), *reversed(trail)]
        key = tuple(trail)
        if key == self._crumb_key:
            return
        self._crumb_key = key
        self.location_menu.clear()
        self.location.setText(trail[-1][1])
        self.location.setToolTip(' › '.join(name for uid, name in trail))
        for uid, title in trail:
            action = self.location_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(uid == e.scope_id)
            action.triggered.connect(
                lambda checked=False, u=uid: self.window.region_panel.navigate(
                    lambda: self.window.region_panel._set_scope(u)
                )
            )

    def close(self):
        self.sync_indicator.set_active(False)
        self.refresh_indicator.set_active(False)
        self.window.view_state_changed.disconnect(self.refresh)
