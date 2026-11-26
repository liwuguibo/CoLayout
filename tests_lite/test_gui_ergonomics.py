# ruff: noqa: F811
from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from colayout_lite.actions import Actions
from colayout_lite.camera import capture as capture_camera
from colayout_lite.canvas_hits import editable_at
from colayout_lite.circuit.models import (
    CircuitContext,
    LayoutConnectivity,
    NativeCheck,
    ViewRef,
)
from colayout_lite.document import save_document
from colayout_lite.editor import Editor
from colayout_lite.model import Object, Shape, Snapshot, Workspace
from colayout_lite.presentation.snapshot_canvas import SnapshotCanvas
from colayout_lite.regions.actions import RegionActions
from colayout_lite.studio_window import StudioWindow
from colayout_lite.tool_popover import ToolPopover
from colayout_lite.workspace_storage import WorkspaceStorage
from tests_lite.test_parasitic_rc import profile, rc


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def make_studio_window(tmp_path, profile):
    root = [None] * 12
    root[4] = 1
    root[5] = [[0, 0], [20000, 20000]]
    root[9] = []
    root[10] = []
    root[11] = []
    wire = Object(
        uid='wire0',
        native_id='wire0',
        kind='path',
        shape=Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, 0), (20000, 0)), width=2000),
    )
    def style_row(layer, packet_name, priority, material, rgb):
        color = ['display', packet_name, *rgb]
        packet = ['display', packet_name, 'blank', 'solid', packet_name, packet_name, 'solid']
        return [
            f'{layer}/drawing',
            [[layer, 'drawing'], packet_name, priority, True, material, packet, color, color, None, None],
        ]

    styles = [
        style_row('wire_a', 'pa', 10, 'metal', [80, 170, 240]),
        style_row('wire_b', 'pb', 20, 'metal', [240, 140, 80]),
        style_row('cut', 'pc', 15, 'cut', [230, 220, 90]),
    ]
    snapshot = Snapshot(
        library='L',
        cell='C',
        library_path='/test/L',
        session='s',
        counter='1',
        dirty=False,
        dbu_per_micron=2000,
        grid=2,
        objects={'wire0': wire},
        pins=[],
        display={'version': 'colayout-display-0.1', 'root': 'root', 'nodes': {'root': root}, 'styles': styles},
        technology_fingerprint='fp',
    )
    schem_ref = ViewRef(library='L', cell='C', view='schematic', session='s', counter='1')
    reference = CircuitContext(
        source=schem_ref,
        pair_id='pair',
        instances={},
        ports={},
        bindings=(),
        native_check=NativeCheck(report='synthetic fixture'),
    )
    connectivity = LayoutConnectivity(
        source=schem_ref.model_copy(update={'view': 'layout'}),
        pair_id='pair',
        instances={},
        ports={},
    )
    w = Workspace(
        baseline=snapshot,
        profile_id=profile.id,
        circuit=reference,
        layout_connectivity=connectivity,
    )
    e = Editor(w, profile)
    # Add overlapping shapes on wire_a (bottom, priority 10) and wire_b (top, priority 20) at (2..6, 2..6) µm
    r_a = Actions(e).execute(
        'create_rect', {'x': 2.0, 'y': 2.0, 'width': 4.0, 'height': 4.0, 'lpp': ['wire_a', 'drawing']}
    )
    r_b = Actions(e).execute(
        'create_rect', {'x': 2.0, 'y': 2.0, 'width': 4.0, 'height': 4.0, 'lpp': ['wire_b', 'drawing']}
    )
    reg_id = RegionActions(e).create((2000, 2000, 18000, 18000), 'amp_core')
    RegionActions(e).add_markup((3000, 3000, 5000, 5000), 'match pair', scope_id=reg_id)
    RegionActions(e).add_keepout((13000, 13000, 17000, 17000), ['wire_b'], 'clock shield')

    doc_path = tmp_path / 'test_doc.colayout.json'
    save_document(doc_path, e.w, e.profile)
    storage = WorkspaceStorage(tmp_path / 'runtime')
    win = StudioWindow(
        doc_path,
        shortcut_path=tmp_path / 'gui-shortcuts.json',
        storage=storage,
    )
    win.show()
    QApplication.processEvents()
    win.fit()
    QApplication.processEvents()
    return win, r_a.uid, r_b.uid, reg_id


def scene_to_viewport(win, x_dbu: float, y_dbu: float) -> QPoint:
    return win.canvas.mapFromScene(QPointF(x_dbu, -y_dbu))


def test_studio_launch_full_canvas_wire_popover_and_parasitic_tabbing(
    app, tmp_path, profile
):
    win, _, _, _ = make_studio_window(tmp_path, profile)
    try:
        actions = win.studio.commands.actions
        # Full-canvas launch: RC dock starts hidden in Studio mode even when RC model exists
        assert win.parasitics.dock.isHidden()
        assert not actions['parasitics'].isChecked()

        # Wire tool opens in a top-bar ToolPopover in Studio mode, not a right QDockWidget
        actions['wire'].trigger()
        QApplication.processEvents()
        assert hasattr(win, 'wire_popup')
        assert isinstance(win.wire_popup, ToolPopover)
        assert win.wire_popup.isVisible()
        assert actions['wire'].isChecked()

        # Starting wire draw dismisses the popover for unobstructed canvas view while staying checked
        win.wire_panel.start(True)
        QApplication.processEvents()
        assert win.wire_panel.capture.active
        assert not win.wire_popup.isVisible()
        assert actions['wire'].isChecked()

        # Esc cancels wire capture and unchecks toolbar action
        QTest.keyClick(win.canvas, Qt.Key.Key_Escape)
        QApplication.processEvents()
        assert not win.wire_panel.capture.active
        assert not actions['wire'].isChecked()

        # Parasitics toggles into right studioTool tab area without shifting camera
        win.studio.set_panel_visible('inspector', True)
        cam_before = capture_camera(win.canvas)
        actions['parasitics'].trigger()
        QApplication.processEvents()
        assert not win.parasitics.dock.isHidden()
        assert win.parasitics.dock.property('studioTool') is True
        assert actions['parasitics'].isChecked()
        cam_after = capture_camera(win.canvas)
        assert cam_after.transform == cam_before.transform
        assert cam_after.bounds == cam_before.bounds
        assert cam_after.center == cam_before.center
        assert cam_after.scroll == cam_before.scroll

        actions['parasitics'].trigger()
        QApplication.processEvents()
        assert win.parasitics.dock.isHidden()
        assert not actions['parasitics'].isChecked()
    finally:
        win.close()


def test_wire_popover_click_through_first_point(
    app, tmp_path, profile
):
    win, _, _, _ = make_studio_window(tmp_path, profile)
    try:
        actions = win.studio.commands.actions
        actions['wire'].trigger()
        QApplication.processEvents()
        assert win.wire_popup.isVisible()
        assert not win.wire_panel.capture.active

        # Clicking the canvas directly starts wire capture and drops the first point
        pos = scene_to_viewport(win, 4000, 4000)
        QTest.mouseClick(win.canvas.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        QApplication.processEvents()
        assert not win.wire_popup.isVisible()
        assert win.wire_panel.capture.active
        assert len(win.wire_panel.capture.draft.points) >= 1
    finally:
        win.close()


def test_layer_palette_hide_solo_clickthrough_marquee_and_snapshot_isolation(
    app, tmp_path, profile
):
    win, uid_a, uid_b, reg_id = make_studio_window(tmp_path, profile)
    try:
        # Enter amp_core scope where uid_a and uid_b live
        win.region_panel._set_scope(reg_id)
        win.fit()
        QApplication.processEvents()

        # Toggle both Hierarchy and Layers in the left splitter; zoom & scene center stay invariant
        cam_before = capture_camera(win.canvas)
        win.studio.panel_actions['hierarchy'].setChecked(True)
        win.studio.panel_actions['layers'].setChecked(True)
        QApplication.processEvents()
        assert not win.studio.left_column.isHidden()
        cam_after = capture_camera(win.canvas)
        assert cam_after.transform == cam_before.transform
        assert abs(cam_after.center.x() - cam_before.center.x()) <= 35
        assert abs(cam_after.center.y() - cam_before.center.y()) <= 35

        center_px = QPointF(scene_to_viewport(win, 8000, 8000))
        # Ensure wire_b has higher priority in styles; pick hits top wire_b before hiding
        assert editable_at(win, center_px) == uid_b
        win.selection_ui.set_ids([uid_b])
        assert uid_b in win.selection_ui.ids()

        rev_before = win.editor.w.revision
        # Hide wire_b via StudioLayerPalette checkbox
        palette_widget = win.studio.layers
        palette_widget._rows['wire_b'].setCheckState(Qt.CheckState.Unchecked)
        QApplication.processEvents()

        assert 'wire_b' in win.hidden_layers
        # Previously selected hidden shape is automatically deselected to prevent blind Delete
        assert uid_b not in win.selection_ui.ids()
        # Click at (8000, 8000) now clicks right through hidden wire_b to wire_a!
        assert editable_at(win, center_px) == uid_a
        # Marquee drag across (2500..13500, 2500..13500) selects wire_a and markup, NEVER hidden wire_b
        p0 = QPointF(scene_to_viewport(win, 2500, 13500))
        p1 = QPointF(scene_to_viewport(win, 13500, 2500))
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress, p0, p0,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        assert win.selection_ui.begin(press)
        win.selection_ui._update_preview(p1)
        preview_ids = set(win.selection_ui.ids())
        win.selection_ui.cancel()
        assert uid_a in preview_ids
        assert uid_b not in preview_ids
        assert win.editor.w.revision == rev_before

        # SnapshotCanvas for Agent remains 100% isolated from human hidden_layers
        snap = SnapshotCanvas(win.editor.w, win.editor.profile, reg_id, 320, 240)
        try:
            assert any(child.isVisible() for child in snap.groups[uid_b].childItems())
        finally:
            snap.close()

        # Double-click Solo mode on wire_a keeps wire_a + connecting cut layer ('cut') visible
        palette_widget.show_all()
        palette_widget.toggle_solo('wire_a')
        assert palette_widget.solo_layer == 'wire_a'
        assert 'wire_a' not in win.hidden_layers
        assert 'wire_b' in win.hidden_layers
        palette_widget.exit_solo()
        assert palette_widget.solo_layer is None
        assert not win.hidden_layers
    finally:
        win.close()


def test_ruler_tool_shortcuts_scope_isolation_and_zero_workspace_mutation(
    app, tmp_path, profile
):
    win, _, _, reg_id = make_studio_window(tmp_path, profile)
    try:
        rev_before = win.editor.w.revision
        ops_before = len(win.editor.w.operations)
        actions = win.studio.commands.actions

        actions['ruler'].trigger()
        QApplication.processEvents()
        assert win.ruler.active
        assert actions['ruler'].isChecked()

        # Place start point, flip to diagonal with Space, and click second point
        QTest.mouseClick(
            win.canvas.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
            pos=scene_to_viewport(win, 1000, 1000),
        )
        QTest.keyClick(win.canvas, Qt.Key.Key_Space)
        assert win.ruler.mode == 'diagonal'
        QTest.mouseClick(
            win.canvas.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
            pos=scene_to_viewport(win, 4000, 5000),
        )
        QApplication.processEvents()
        # Continuous ruler mode stays active after placing the first segment
        assert win.ruler.active
        assert actions['ruler'].isChecked()
        rulers = win.ruler.current_rulers()
        assert len(rulers) == 1
        assert rulers[0].mode == 'diagonal'
        assert abs(rulers[0].p0[0] - 1000) <= 60 and abs(rulers[0].p0[1] - 1000) <= 60
        assert abs(rulers[0].p1[0] - 4000) <= 60 and abs(rulers[0].p1[1] - 5000) <= 60
        assert win.editor.w.revision == rev_before and len(win.editor.w.operations) == ops_before

        # Place a second ruler segment without pressing K in between
        QTest.mouseClick(
            win.canvas.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
            pos=scene_to_viewport(win, 5000, 1000),
        )
        QTest.mouseClick(
            win.canvas.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
            pos=scene_to_viewport(win, 9000, 3000),
        )
        QApplication.processEvents()
        assert len(win.ruler.current_rulers()) == 2

        # A plain right-click with no active starting point exits ruler mode and does not open a menu
        QTest.mouseClick(
            win.canvas.viewport(),
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
            pos=scene_to_viewport(win, 5000, 5000),
        )
        QApplication.processEvents()
        assert not win.ruler.active
        assert not actions['ruler'].isChecked()
        assert len(win.ruler.current_rulers()) == 2

        # Child scope has isolated ruler list
        win.region_panel._set_scope(reg_id)
        assert win.ruler.current_rulers() == []
        win.region_panel._set_scope(None)
        assert len(win.ruler.current_rulers()) == 2

        actions['clear_rulers'].trigger()
        assert win.ruler.current_rulers() == []
    finally:
        win.close()


def test_middle_button_pan_anti_stick_and_focus_out_cleanup(
    app, tmp_path, profile
):
    win, _, _, _ = make_studio_window(tmp_path, profile)
    try:
        from colayout_lite.camera import capture as capture_camera

        cam_before = capture_camera(win.canvas)
        pos = scene_to_viewport(win, 5000, 5000)
        # Middle-button press starts pan candidate; immediate move pans the view
        QTest.mousePress(win.canvas.viewport(), Qt.MouseButton.MiddleButton, pos=pos)
        QApplication.processEvents()
        assert win.canvas._pan_last is not None
        assert win.canvas._pan_button == Qt.MouseButton.MiddleButton
        QTest.mouseMove(win.canvas.viewport(), pos + QPoint(30, 20), delay=-1)
        QApplication.processEvents()
        cam_after = capture_camera(win.canvas)
        assert cam_after.center != cam_before.center

        # Simulate a swallowed MouseButtonRelease: a no-button move heals the state
        healed_pos = pos + QPoint(40, 30)
        no_buttons = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(healed_pos),
            QPointF(healed_pos),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        win.canvas.mouseMoveEvent(no_buttons)
        QApplication.processEvents()
        assert win.canvas._pan_last is None
        assert win.canvas._pan_button is None
        cam_healed = capture_camera(win.canvas)
        win.canvas.mouseMoveEvent(no_buttons)
        QApplication.processEvents()
        assert capture_camera(win.canvas).center == cam_healed.center

        # A second press while already panning must not overwrite _pan_button
        QTest.mousePress(win.canvas.viewport(), Qt.MouseButton.MiddleButton, pos=pos)
        QApplication.processEvents()
        assert win.canvas._pan_button == Qt.MouseButton.MiddleButton
        QTest.mousePress(win.canvas.viewport(), Qt.MouseButton.RightButton, pos=pos)
        QApplication.processEvents()
        assert win.canvas._pan_button == Qt.MouseButton.MiddleButton
        QTest.mouseRelease(win.canvas.viewport(), Qt.MouseButton.MiddleButton, pos=pos)
        QApplication.processEvents()
        assert win.canvas._pan_last is None

        # Focus out and window deactivation clean any lingering pan state
        from PySide6.QtGui import QFocusEvent

        QTest.mousePress(win.canvas.viewport(), Qt.MouseButton.MiddleButton, pos=pos)
        QApplication.processEvents()
        assert win.canvas._pan_last is not None
        QApplication.sendEvent(win.canvas, QFocusEvent(QEvent.Type.FocusOut))
        QApplication.processEvents()
        assert win.canvas._pan_last is None

        QTest.mousePress(win.canvas.viewport(), Qt.MouseButton.MiddleButton, pos=pos)
        QApplication.processEvents()
        assert win.canvas._pan_last is not None
        QApplication.sendEvent(win, QEvent(QEvent.Type.WindowDeactivate))
        QApplication.processEvents()
        assert win.canvas._pan_last is None
    finally:
        win.close()


def test_footer_live_coords_conductor_pill_and_copy_task_context(
    app, tmp_path, profile
):
    win, _, _, reg_id = make_studio_window(tmp_path, profile)
    try:
        # Conductor lock/move pill two-way sync
        pill = win.studio.conductor_toggle
        assert not pill.isChecked()
        assert pill.text() == '锁线'
        pill.click()
        QApplication.processEvents()
        assert pill.isChecked()
        assert win.selection_ui.move_conductors is True
        assert pill.text() == '移线'
        win.selection_ui.move_box.setChecked(False)
        QApplication.processEvents()
        assert not pill.isChecked() and pill.text() == '锁线'

        # Live coordinate and active ruler delta readout (dbu_per_micron = 2000)
        win.canvas.cursor_moved.emit(5000, 7000)
        assert 'X 2.5  Y 3.5 µm' == win.studio.coords.text()

        win.ruler.start_or_toggle()
        QTest.mouseClick(
            win.canvas.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
            pos=scene_to_viewport(win, 1000, 1000),
        )
        p_move = QPointF(scene_to_viewport(win, 5000, 1000))
        move_ev = QMouseEvent(
            QEvent.Type.MouseMove, p_move, p_move,
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.ShiftModifier,
        )
        win.ruler.eventFilter(win.canvas.viewport(), move_ev)
        assert 'Δ(' in win.studio.coords.text() and 'L=' in win.studio.coords.text()
        win.ruler.cancel_capture()

        # Copy task context formats selected region + guides cleanly to clipboard
        win.selection_ui.set_ids([reg_id])
        win.studio.commands.actions['copy_task_context'].trigger()
        clip = QApplication.clipboard().text()
        assert 'amp_core' in clip
        assert reg_id in clip
        win.region_panel._set_scope(None)
        win.selection_ui.set_ids([reg_id])
        win.studio.commands.actions['copy_task_context'].trigger()
        clip_top = QApplication.clipboard().text()
        assert 'amp_core' in clip_top and reg_id in clip_top
    finally:
        win.close()


def test_wire_modal_right_click_undo_and_right_drag_pan_no_menu(
    app, tmp_path, profile
):
    win, _, _, _ = make_studio_window(tmp_path, profile)
    real_context_menu = win.human.context_menu
    menu_calls = []
    def spy_context_menu(pos):
        menu_calls.append(pos)
    win.human.context_menu = spy_context_menu
    try:
        actions = win.studio.commands.actions
        actions['wire'].trigger()
        win.wire_panel.start(True)
        QApplication.processEvents()
        assert win.wire_panel.capture.active

        p0 = scene_to_viewport(win, 4000, 4000)
        p1 = scene_to_viewport(win, 8000, 4000)
        QTest.mouseClick(win.canvas.viewport(), Qt.MouseButton.LeftButton, pos=p0)
        QTest.mouseClick(win.canvas.viewport(), Qt.MouseButton.LeftButton, pos=p1)
        QApplication.processEvents()
        assert len(win.wire_panel.capture.draft.points) == 2

        from colayout_lite.camera import capture as capture_camera
        cam_before = capture_camera(win.canvas)

        # Right-drag over threshold must pan the view, not delete anchors or open a menu
        QTest.mousePress(win.canvas.viewport(), Qt.MouseButton.RightButton, pos=p0)
        drag_end = p0 + QPoint(40, 30)
        QTest.mouseMove(win.canvas.viewport(), drag_end, delay=-1)
        QApplication.processEvents()
        assert win.canvas._pan_started
        QTest.mouseRelease(win.canvas.viewport(), Qt.MouseButton.RightButton, pos=drag_end)
        QApplication.processEvents()
        assert capture_camera(win.canvas).center != cam_before.center
        assert len(win.wire_panel.capture.draft.points) == 2
        assert not menu_calls

        # A plain right-click undoes the last anchor without opening a menu
        QTest.mouseClick(win.canvas.viewport(), Qt.MouseButton.RightButton, pos=p1)
        QApplication.processEvents()
        assert len(win.wire_panel.capture.draft.points) == 1
        assert not menu_calls
    finally:
        win.human.context_menu = real_context_menu
        win.close()


def test_wire_modal_hotkeys_work_when_focus_leaves_canvas(
    app, tmp_path, profile
):
    win, _, _, _ = make_studio_window(tmp_path, profile)
    try:
        actions = win.studio.commands.actions
        actions['wire'].trigger()
        win.wire_panel.start(True)
        QApplication.processEvents()
        assert win.wire_panel.capture.active
        assert win.wire_panel.capture.posture == 'hv'

        p0 = scene_to_viewport(win, 4000, 4000)
        QTest.mouseClick(win.canvas.viewport(), Qt.MouseButton.LeftButton, pos=p0)
        QApplication.processEvents()
        assert len(win.wire_panel.capture.draft.points) == 1

        # Open the left layer palette and move focus into its QListWidget away from the canvas
        win.studio.panel_actions['layers'].setChecked(True)
        QApplication.processEvents()
        layer_list = win.studio.layers.list
        layer_list.setFocus()
        QApplication.processEvents()
        assert layer_list not in (win.canvas, win.canvas.viewport())

        from PySide6.QtGui import QKeyEvent

        def send_key(key):
            target = QApplication.focusWidget()
            if target is None or target in (win.canvas, win.canvas.viewport()):
                target = layer_list
            ev = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target, ev)
            QApplication.processEvents()

        # Space cycles posture even though focus is not on the canvas
        send_key(Qt.Key.Key_Space)
        assert win.wire_panel.capture.posture == 'vh'

        # ] steps up the metal stack
        send_key(Qt.Key.Key_BracketRight)
        assert win.wire_panel.layer.currentData() == 'wire_b'

        # Backspace undoes the last anchor without deleting canvas objects
        rev_before = win.editor.w.revision
        send_key(Qt.Key.Key_Backspace)
        assert len(win.wire_panel.capture.draft.points) == 0
        assert win.editor.w.revision == rev_before

        # Escape cancels wire capture
        send_key(Qt.Key.Key_Escape)
        assert not win.wire_panel.capture.active
        assert not actions['wire'].isChecked()
    finally:
        win.close()


def test_auto_marquee_arrow_keys_and_shift_orthogonal_drag(
    app, tmp_path, profile
):
    win, uid_a, uid_b, reg_id = make_studio_window(tmp_path, profile)
    try:
        # Default marquee mode is 'auto' with three choices in the UI
        assert win.selection_ui.mode == 'auto'
        assert win.selection_ui.mode_combo.count() == 3
        assert win.studio.selection_mode.count() == 3

        # wire0 spans (0,-1000)..(20000,1000); a small box intersects but does not contain it.
        p_left = QPointF(scene_to_viewport(win, 1000, 3000))   # scene (1000,-3000)
        p_right = QPointF(scene_to_viewport(win, 3000, -3000))  # scene (3000,3000)

        # Left->right drag uses effective 'contains' mode
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress, p_left, p_left,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        assert win.selection_ui.begin(press)
        win.selection_ui._update_preview(p_right)
        assert 'wire0' not in win.selection_ui.ids()
        win.selection_ui.cancel()

        # Right->left drag uses effective 'intersects' mode
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress, p_right, p_right,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        assert win.selection_ui.begin(press)
        win.selection_ui._update_preview(p_left)
        assert 'wire0' in win.selection_ui.ids()
        win.selection_ui.cancel()

        # Arrow keys pan the camera without mutating the workspace
        win.selection_ui.set_ids([uid_a])
        from PySide6.QtGui import QKeyEvent
        from colayout_lite.camera import capture as capture_camera
        cam_before = capture_camera(win.canvas)
        rev_before = win.editor.w.revision
        wire_before = win.editor.w.baseline.objects['wire0'].shape.points
        win.canvas.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier)
        )
        QApplication.processEvents()
        cam_after = capture_camera(win.canvas)
        dx = cam_after.center.x() - cam_before.center.x()
        dy = cam_after.center.y() - cam_before.center.y()
        assert dx > 1000 and abs(dy) < abs(dx) * 0.1
        win.canvas.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
        )
        QApplication.processEvents()
        cam_up = capture_camera(win.canvas)
        # World Y increases upward, which is decreasing scene Y in our mapping.
        assert cam_up.center.y() < cam_after.center.y()
        assert win.editor.w.revision == rev_before
        assert win.editor.w.baseline.objects['wire0'].shape.points == wire_before

        # Shift+drag on a rectangle locks to the dominant axis instead of entering marquee
        win.selection_ui.move_box.setChecked(True)
        QApplication.processEvents()
        win.selection_ui.set_ids([uid_a])
        QApplication.processEvents()
        center = QPointF(scene_to_viewport(win, 8000, 8000))
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress, center, center,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
        )
        assert win.human.begin(press)
        move = QMouseEvent(
            QEvent.Type.MouseMove, center + QPointF(80, 30), center + QPointF(80, 30),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
        )
        assert win.human.move(move)
        dx, dy = win.human.armed['delta']
        assert dx != 0 and dy == 0
        win.human.cancel()

        # Shift+drag on a movable target when a read-only wire is already selected never raises LiteError
        win.region_panel._set_scope(reg_id)
        win.fit()
        QApplication.processEvents()
        locked_wire = win.editor.w.baseline.objects['wire0'].model_copy(
            update={'read_only_reason': 'locked'}
        )
        win.editor.w = win.editor.w.model_copy(
            update={
                'baseline': win.editor.w.baseline.model_copy(
                    update={'objects': {**win.editor.w.baseline.objects, 'wire0': locked_wire}}
                )
            }
        )
        win.selection_ui.set_ids(['wire0'])
        inner_center = QPointF(scene_to_viewport(win, 8000, 8000))
        inner_press = QMouseEvent(
            QEvent.Type.MouseButtonPress, inner_center, inner_center,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
        )
        assert win.human.begin(inner_press)
        assert win.human.armed['ids'] == [uid_b]
        win.human.cancel()

        # GuardPanel PathCapture preserves middle-button pan and restores CrossCursor on release
        from colayout_lite.guard_panel import show as show_guard
        show_guard(win)
        guard_panel = win.guard_panel
        win.guard_popup.hide()
        guard_panel.capture.start()
        assert win.canvas.cursor().shape() == Qt.CursorShape.CrossCursor
        p_mid = scene_to_viewport(win, 6000, 6000)
        QTest.mousePress(win.canvas.viewport(), Qt.MouseButton.MiddleButton, pos=p_mid)
        QTest.mouseMove(win.canvas.viewport(), p_mid + QPoint(25, 15), delay=-1)
        QTest.mouseRelease(
            win.canvas.viewport(), Qt.MouseButton.MiddleButton, pos=p_mid + QPoint(25, 15)
        )
        QApplication.processEvents()
        assert win.canvas._pan_last is None
        assert win.canvas.cursor().shape() == Qt.CursorShape.CrossCursor
        guard_panel.capture.cancel()
    finally:
        win.close()
