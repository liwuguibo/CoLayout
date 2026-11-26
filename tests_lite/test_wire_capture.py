# ruff: noqa: F811
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from colayout_lite.wire_capture import DraftRoute
from colayout_lite.wire_panel import WirePanel
from tests_lite.test_parasitic_view import Host, Selection, app, host_window, wait_until
from tests_lite.test_parasitic_rc import profile, rc


def test_multilayer_draft_shapes_and_resistance(profile):
    draft = DraftRoute(points=((0, 0), (10000, 0), (10000, 10000)), layers=('wire_a', 'wire_b'),
        widths_um=(.5, .5), via_definitions=('JOIN',), cut_rows=2, cut_columns=2)
    shapes = draft.shapes(profile)
    assert [shape.kind for shape in shapes] == ['path', 'path', 'stdVia']
    geometry = draft.geometry(profile)
    assert geometry.simple_series
    assert sum(row.ohms for row in geometry.resistances) == pytest.approx(4.5)
    assert len(geometry.conductors) > 2
    with pytest.raises(ValueError, match='Missing via'):
        DraftRoute(draft.points, draft.layers, draft.widths_um, ()).shapes(profile)


def prepare_wire_host(profile, monkeypatch):
    host = host_window(profile, monkeypatch)
    host.styles = {}
    host.busy = False
    host.status = QLabel()
    host.drc = SimpleNamespace(pick=SimpleNamespace(setChecked=lambda _: None))
    selection = host.human
    selection.cancel = lambda: None
    selection.escape = lambda: None
    selection.set_ids = lambda ids: setattr(selection, 'value', list(ids))
    host.selection_ui = selection
    host.canvas.setSceneRect(QRectF(-5000, -15000, 30000, 30000))
    host.canvas.fitInView(host.canvas.sceneRect(), Qt.AspectRatioMode.IgnoreAspectRatio)
    host.wire_panel = WirePanel(host)
    return host


def canvas_point(host, x, y):
    return host.canvas.mapFromScene(QPointF(x, -y))


def mouse_move(host, x, y):
    point = canvas_point(host, x, y)
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(point),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    host.wire_panel.capture.eventFilter(host.canvas.viewport(), event)


def test_unconfirmed_turn_layer_change_via_preview_and_single_commit(app, profile, monkeypatch):
    import colayout_lite.parasitic_workspace as parasitic_workspace

    calls = []
    real_eval = parasitic_workspace.evaluate_parasitics_outcome

    def spy_eval(*args, **kwargs):
        calls.append(kwargs.get('uids'))
        return real_eval(*args, **kwargs)

    monkeypatch.setattr('colayout_lite.parasitic_view.evaluate_parasitics_outcome', spy_eval)
    host = prepare_wire_host(profile, monkeypatch)
    panel = host.wire_panel
    view = host.parasitics
    try:
        panel.start(True)
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.LeftButton, pos=canvas_point(host, 0, 0))
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.LeftButton, pos=canvas_point(host, 10000, 0))
        panel.layer.setCurrentIndex(panel.layer.findData('wire_b'))
        panel.rows.setValue(2)
        panel.columns.setValue(2)
        mouse_move(host, 10000, -10000)
        assert panel.draft is not None
        assert len(panel.draft.shapes(profile)) == 3
        assert host.editor.w.revision == 0 and not host.editor.w.operations
        # Interactive mouse motion and wire commit trigger zero unsolicited RC work
        assert calls == []
        assert view.estimate is None

        panel.capture.end()
        assert host.editor.w.revision == 1
        assert len(host.editor.w.operations) == 3
        assert len(host.editor._undo) == 1
        assert calls == []
        assert view.estimate is None
        assert panel.inspect_rc_button.isEnabled()

        # Clicking "查看本次走线 RC" after routing calculates RC + delta on demand
        panel.inspect_rc_button.click()
        assert wait_until(app, lambda: view.estimate is not None)
        assert len(calls) == 1
        assert view.estimate.ground_capacitance_fF > 0
        assert view.estimate.coupling_capacitance_fF > 0
        assert view.panel.delta.text() not in ('', '—')
        assert '排队中' not in view.panel.status.text()
    finally:
        host.close()


def test_escape_cancels_uncommitted_wire(app, profile, monkeypatch):
    host = prepare_wire_host(profile, monkeypatch)
    panel = host.wire_panel
    try:
        panel.start(True)
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.LeftButton, pos=canvas_point(host, 0, 0))
        mouse_move(host, 10000, 0)
        panel.cancel_draw()
        assert host.editor.w.revision == 0 and not host.editor.w.operations
        assert host.parasitics.estimate is None
    finally:
        host.close()


def test_move_on_demand_after_commit_produces_negative_delta(app, profile, monkeypatch):
    from colayout_lite.human_edit import HumanEdit

    host = host_window(profile, monkeypatch)
    view = host.parasitics
    try:
        before = host.editor.w
        HumanEdit(host.editor).transform(['wire'], delta=(0, -1000))
        assert view.estimate is None
        assert view.evaluate_now(['wire'], baseline_workspace=before, baseline_target_uids=['wire'])
        assert wait_until(app, lambda: view.estimate is not None and 'fF' in view.panel.delta.text())
        text = view.panel.delta.text()
        assert '覆盖不一致' not in text
        parts = text.split('/')
        assert float(parts[-1].replace('fF', '').strip()) < 0
    finally:
        host.close()


def test_missing_via_preview_never_blocks_gesture_or_causes_partial_commit(app, profile, monkeypatch):
    host = prepare_wire_host(profile, monkeypatch)
    panel = host.wire_panel
    try:
        panel.start(True)
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.LeftButton, pos=canvas_point(host, 0, 0))
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.LeftButton, pos=canvas_point(host, 10000, 0))
        panel.layer.setCurrentIndex(panel.layer.findData('wire_b'))
        panel.via.clear()
        mouse_move(host, 10000, -10000)
        assert 'Missing via' in panel.message.text() or '走线草案提示' in panel.message.text()
        assert host.editor.w.revision == 0
        panel.capture.end()
        assert host.editor.w.revision == 0 and not host.editor.w.operations
    finally:
        host.close()


def test_lbend_posture_flip_layer_step_and_right_click_undo(app, profile, monkeypatch):
    host = prepare_wire_host(profile, monkeypatch)
    panel = host.wire_panel
    try:
        panel.start(True)
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.LeftButton, pos=canvas_point(host, 0, 0))
        mouse_move(host, 10000, -10000)
        assert len(panel.draft.points) == 3
        tx, ty = panel.draft.points[-1]
        assert abs(tx - 10000) <= 40 and abs(ty - (-10000)) <= 40
        assert panel.draft.points == ((0, 0), (tx, 0), (tx, ty))

        # Space flips L-bend elbow from horizontal-first ('hv') to vertical-first ('vh')
        QTest.keyClick(host.canvas, Qt.Key.Key_Space)
        assert panel.capture.posture == 'vh'
        assert panel.draft.points == ((0, 0), (0, ty), (tx, ty))

        # Commit the 2-segment L-bend in one click, then right-click undoes the whole L-bend
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.LeftButton, pos=canvas_point(host, 10000, -10000))
        assert panel.capture.draft.points == ((0, 0), (0, ty), (tx, ty))
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.RightButton, pos=canvas_point(host, 10000, -10000))
        assert panel.capture.draft.points == ((0, 0),)

        # Re-click L-bend, step up metal stack with ']', and commit atomically
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.LeftButton, pos=canvas_point(host, 10000, -10000))
        QTest.keyClick(host.canvas, Qt.Key.Key_BracketRight)
        assert panel.layer.currentData() == 'wire_b'
        assert panel.via.currentData() == 'JOIN'
        mouse_move(host, 20000, -10000)
        shapes = panel.draft.shapes(profile)
        assert [s.kind for s in shapes] == ['path', 'path', 'path', 'stdVia']
        panel.capture.end()
        assert host.editor.w.revision == 1
        assert len(host.editor.w.operations) == 4
        assert len(host.editor._undo) == 1
    finally:
        host.close()


def test_wire_capture_undo_step_and_right_click_api(app, profile, monkeypatch):
    host = prepare_wire_host(profile, monkeypatch)
    panel = host.wire_panel
    try:
        panel.start(True)
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.LeftButton, pos=canvas_point(host, 0, 0))
        QTest.mouseClick(host.canvas.viewport(), Qt.MouseButton.LeftButton, pos=canvas_point(host, 10000, 0))
        assert len(panel.capture.draft.points) == 2

        # undo_step pops the last committed anchor
        assert panel.capture.undo_step()
        assert len(panel.capture.draft.points) == 1
        assert panel.capture.active

        # right_click with anchors left calls undo_step, with empty draft calls cancel
        panel.capture.right_click()
        assert len(panel.capture.draft.points) == 0
        assert panel.capture.active
        panel.capture.right_click()
        assert not panel.capture.active
    finally:
        host.close()
