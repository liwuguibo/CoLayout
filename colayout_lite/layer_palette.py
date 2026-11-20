"""Compact view-only layer visibility palette (LSW) with Solo mode."""

from __future__ import annotations

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .wire_defaults import metal_stack

_ROLE_LABELS = {
    'metal': '金属',
    'cut': '切孔',
    'poly': '多晶',
    'diff': '有源',
    'nwell': 'N阱',
    'pwell': 'P阱',
    'nplus': 'N注入',
    'pplus': 'P注入',
}


def _swatch_icon(style: dict | None, *, is_cut: bool = False) -> QIcon:
    pix = QPixmap(14, 14)
    pix.fill(QColor('#14171c'))
    painter = QPainter(pix)
    try:
        outline = QColor(*(style['outline_rgb'] if style else (154, 164, 178)))
        fill_rgb = style['fill_rgb'] if style else (110, 120, 134)
        fill = QColor(*fill_rgb)
        fill_style = (style.get('fill_style') or 'outline') if style else 'outline'
        if fill_style not in ('outline', 'none'):
            fill.setAlpha(220 if 'solid' in fill_style.lower() else 145)
            painter.fillRect(1, 1, 12, 12, fill)
        else:
            fill.setAlpha(65)
            painter.fillRect(1, 1, 12, 12, fill)
        painter.setPen(QPen(outline, 1))
        painter.drawRect(0, 0, 13, 13)
        if is_cut:
            painter.drawLine(2, 2, 11, 11)
            painter.drawLine(11, 2, 2, 11)
    finally:
        painter.end()
    return QIcon(pix)


class StudioLayerPalette(QWidget):
    """Left-column layer visibility and active-drawing-layer selector.

    Visibility changes are strictly view-only (`Window.hidden_layers`); they
    never mutate `Workspace`, `TechnologyProfile`, DRC, sync, or Agent snapshots.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setObjectName('studioLayers')
        self._structure_key = None
        self._rows: dict[str, QListWidgetItem] = {}
        self._layer_roles: dict[str, str | None] = {}
        self.solo_layer: str | None = None
        self._pre_solo_hidden: set[str] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        title = QLabel('图层')
        title.setObjectName('studioTitle')
        layout.addWidget(title)

        self.summary = QLabel('未载入工艺')
        self.summary.setObjectName('studioSubtitle')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)
        self.show_all_button = QPushButton('全部')
        self.show_all_button.setToolTip('显示全部图层')
        self.show_all_button.clicked.connect(self.show_all)
        self.metals_only_button = QPushButton('仅金属')
        self.metals_only_button.setToolTip('仅显示金属层及其连接过孔切孔层')
        self.metals_only_button.clicked.connect(self.show_metals_only)
        self.exit_solo_button = QPushButton('退出独显')
        self.exit_solo_button.setToolTip('恢复双击独显前的图层显隐状态')
        self.exit_solo_button.setEnabled(False)
        self.exit_solo_button.clicked.connect(self.exit_solo)
        for btn in (self.show_all_button, self.metals_only_button, self.exit_solo_button):
            actions.addWidget(btn)
        layout.addLayout(actions)

        self.list = QListWidget()
        self.list.setObjectName('studioLayerList')
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.itemClicked.connect(self._on_item_clicked)
        self.list.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.list, 1)

        self.hint = QLabel('勾选显隐 · 单击联动绘图层 · 双击独显')
        self.hint.setObjectName('studioMuted')
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.refresh()

    def _ordered_layer_entries(self) -> list[tuple[str, str | None, dict | None]]:
        e = self.window.editor
        if not e:
            return []
        profile = e.profile
        roles: dict[str, str | None] = {}
        profile_order: list[str] = []
        for row in profile.layers:
            name = row['layer']
            if name.startswith('__'):
                continue
            if name not in roles:
                profile_order.append(name)
                roles[name] = row.get('material')
            elif row.get('purpose') == 'drawing' and row.get('material'):
                roles[name] = row.get('material')
        styles_by_layer: dict[str, dict] = {}
        for style in self.window.styles.values():
            lpp = style.get('lpp') or ('', '')
            name, purpose = lpp[0], lpp[1] if len(lpp) > 1 else ''
            if not name or name.startswith('__'):
                continue
            if name not in roles:
                profile_order.append(name)
                roles[name] = 'cut' if style.get('is_cut') else None
            if purpose == 'drawing' or name not in styles_by_layer:
                styles_by_layer[name] = style
        # Only display layers that actually exist in the current scene styles or writable/known roles,
        # avoiding hundreds of unused marker layers when a raw PDK techfile is loaded.
        active_names = (
            {style['lpp'][0] for style in self.window.styles.values() if style.get('lpp')}
            | {layer for layer, _ in profile.writable_lpps}
            | {name for name, role in roles.items() if role in _ROLE_LABELS}
        )
        candidates = [name for name in profile_order if name in active_names and not name.startswith('__')]
        try:
            stack = list(metal_stack(profile, profile.editor_defaults.get('metal_stack')))
        except Exception:
            stack = []
        stack_index = {name: idx for idx, name in enumerate(stack)}
        order_index = {name: idx for idx, name in enumerate(candidates)}

        def sort_key(name: str):
            role = roles.get(name)
            if role == 'metal':
                return (0, stack_index.get(name, 1000 + order_index[name]))
            if role == 'cut':
                return (1, order_index[name])
            if role in ('poly', 'diff'):
                return (2, order_index[name])
            if role in ('nwell', 'pwell', 'nplus', 'pplus'):
                return (3, order_index[name])
            return (4, order_index[name])

        ordered = sorted(candidates, key=sort_key)
        return [(name, roles.get(name), styles_by_layer.get(name)) for name in ordered]

    def all_layers(self) -> list[str]:
        return list(self._rows)

    def connecting_cuts(self, metal_layer: str) -> set[str]:
        e = self.window.editor
        if not e:
            return set()
        cuts = set()
        for vd in e.profile.vias.values():
            if vd.get('layer1') == metal_layer or vd.get('layer2') == metal_layer:
                cut = vd.get('cut_layer')
                if cut:
                    cuts.add(cut)
        return cuts

    def refresh(self):
        e = self.window.editor
        if not e:
            self._structure_key = None
            self._rows.clear()
            self._layer_roles.clear()
            self.solo_layer = None
            self._pre_solo_hidden = None
            with QSignalBlocker(self.list):
                self.list.clear()
            self.summary.setText('未载入工艺')
            self.exit_solo_button.setEnabled(False)
            return

        entries = self._ordered_layer_entries()
        structure_key = (
            id(e.profile),
            tuple((name, role, id(style)) for name, role, style in entries),
        )
        if structure_key != self._structure_key:
            self._structure_key = structure_key
            prior_current = self.list.currentItem()
            selected_name = prior_current.data(Qt.ItemDataRole.UserRole) if prior_current else None
            self._rows.clear()
            self._layer_roles = {name: role for name, role, _ in entries}
            if self.solo_layer not in self._layer_roles:
                self.solo_layer = None
                self._pre_solo_hidden = None
            with QSignalBlocker(self.list):
                self.list.clear()
                for name, role, style in entries:
                    is_cut = role == 'cut' or bool(style and style.get('is_cut'))
                    item = QListWidgetItem(_swatch_icon(style, is_cut=is_cut), '')
                    item.setData(Qt.ItemDataRole.UserRole, name)
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    self.list.addItem(item)
                    self._rows[name] = item
                if selected_name in self._rows:
                    self.list.setCurrentItem(self._rows[selected_name])

        hidden = self.window.hidden_layers
        if self.solo_layer and self.solo_layer in hidden:
            self.solo_layer = None
            self._pre_solo_hidden = None
        with QSignalBlocker(self.list):
            for name, item in self._rows.items():
                role = self._layer_roles.get(name)
                role_tag = f'  · {_ROLE_LABELS[role]}' if role in _ROLE_LABELS else ''
                solo_tag = '  [独显]' if self.solo_layer == name else ''
                item.setText(f'{name}{role_tag}{solo_tag}')
                item.setCheckState(
                    Qt.CheckState.Unchecked if name in hidden else Qt.CheckState.Checked
                )
                writable = (name, 'drawing') in e.profile.writable_lpps
                tip = f'{name}' + (f' ({_ROLE_LABELS[role]})' if role in _ROLE_LABELS else '')
                tip += '\n可绘图层（单击同步到画线/矩形/铺铜面板）' if writable else '\n只读显示层'
                tip += '\n双击切换独显模式'
                item.setToolTip(tip)

        total = len(self._rows)
        shown = sum(1 for name in self._rows if name not in hidden)
        if self.solo_layer:
            cuts = sorted(c for c in self.connecting_cuts(self.solo_layer) if c in self._rows and c not in hidden)
            extra = f" (+{'/'.join(cuts)})" if cuts else ''
            self.summary.setText(f'独显 {self.solo_layer}{extra}')
        elif shown == total:
            self.summary.setText(f'全部显示 · {total} 层')
        else:
            self.summary.setText(f'已显示 {shown} / {total} 层')
        self.exit_solo_button.setEnabled(self.solo_layer is not None)

    def _on_item_changed(self, _item: QListWidgetItem):
        hidden = {
            name
            for name, row in self._rows.items()
            if row.checkState() != Qt.CheckState.Checked
        }
        self.solo_layer = None
        self._pre_solo_hidden = None
        self.window.set_hidden_layers(hidden)
        self.refresh()

    def _on_item_clicked(self, item: QListWidgetItem):
        name = item.data(Qt.ItemDataRole.UserRole)
        if name and name not in self.window.hidden_layers:
            self._sync_active_drawing_layer(name)

    def _on_item_double_clicked(self, item: QListWidgetItem):
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            self.toggle_solo(name)

    def toggle_solo(self, layer_name: str):
        if layer_name not in self._rows:
            return
        if self.solo_layer == layer_name:
            self.exit_solo()
            return
        if self.solo_layer is None:
            self._pre_solo_hidden = set(self.window.hidden_layers)
        self.solo_layer = layer_name
        keep = {layer_name}
        if self._layer_roles.get(layer_name) == 'metal':
            keep.update(self.connecting_cuts(layer_name))
        hidden = set(self._rows) - keep
        self.window.set_hidden_layers(hidden)
        self._sync_active_drawing_layer(layer_name)
        self.refresh()
        self.window.status.setText(f'图层独显：{layer_name} · 再次双击或点“退出独显”恢复')

    def exit_solo(self):
        if self.solo_layer is None:
            return
        restored = set(self._pre_solo_hidden or ())
        self.solo_layer = None
        self._pre_solo_hidden = None
        self.window.set_hidden_layers(restored)
        self.refresh()
        self.window.status.setText('已恢复独显前的图层显示状态。')

    def show_all(self):
        self.solo_layer = None
        self._pre_solo_hidden = None
        self.window.set_hidden_layers(set())
        self.refresh()
        self.window.status.setText('已显示全部图层。')

    def show_metals_only(self):
        if not self._rows:
            return
        self.solo_layer = None
        self._pre_solo_hidden = None
        keep = {name for name, role in self._layer_roles.items() if role == 'metal'}
        for metal in list(keep):
            keep.update(self.connecting_cuts(metal))
        self.window.set_hidden_layers(set(self._rows) - keep)
        self.refresh()
        self.window.status.setText('已切换为仅显示金属与过孔切孔层。')

    def _sync_active_drawing_layer(self, layer_name: str):
        e = self.window.editor
        if not e:
            return
        target = (layer_name, 'drawing')
        if target not in e.profile.writable_lpps:
            return
        wire = getattr(self.window, 'wire_panel', None)
        if wire is not None and getattr(wire, 'layer', None) is not None:
            idx = wire.layer.findData(layer_name)
            if idx >= 0 and wire.layer.currentIndex() != idx:
                wire.layer.setCurrentIndex(idx)
        for attr in ('rectangle_panel', 'pour_panel'):
            panel = getattr(self.window, attr, None)
            combo = getattr(panel, 'layers', None)
            if combo is None:
                continue
            idx = combo.findData(target)
            if idx >= 0 and combo.currentIndex() != idx:
                combo.setCurrentIndex(idx)
