"""Metal pour preserves explicit connections, obstacles, grid and atomic undo."""

import pytest
from shapely import set_precision
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from colayout_lite.drc.models import DrcProfile, Rule
from colayout_lite.editor import Editor, projected
from colayout_lite.layout_helpers.service import execute
from colayout_lite.model import LiteError, Object, Placement, Shape, Snapshot, Workspace
from colayout_lite.technology import TechnologyProfile


@pytest.fixture
def profile():
    return TechnologyProfile(
        id='foreign', label='Synthetic technology', native_fingerprint='fp', dbu_per_micron=2000, grid=2,
        writable_lpps=(('wire_a', 'drawing'), ('wire_b', 'drawing')),
        layers=[dict(layer=name, purpose='drawing', material='metal') for name in ['wire_a', 'wire_b']],
        vias={'JOIN': dict(kind='stdViaDef', layer1='wire_a', layer2='wire_b', cut_layer='cut_x', implant1=None, implant2=None,
            defaults=dict(cutWidth=200, cutHeight=240, cutRows=1, cutColumns=1, cutSpacing=[200, 220], layer1Enc=[80, 100],
                layer2Enc=[100, 120], layer1Offset=[0, 0], layer2Offset=[0, 0], originOffset=[0, 0]))},
    )


@pytest.fixture
def editor(profile):
    instance = Object(uid='inst', native_id='old', kind='inst', placement=Placement(origin=(0, 0)), attributes={'master': ['other', 'device', 'layout']})
    snapshot = Snapshot(library='L', cell='C', library_path='/test/L', session='s', counter='1', dirty=False,
        dbu_per_micron=2000, grid=2, objects={'inst': instance}, technology_fingerprint='fp')
    return Editor(Workspace(baseline=snapshot, profile_id=profile.id), profile)


def pour(editor, **overrides):
    params = {
        'bounds_um': [0, 0, 10, 10],
        'lpp': ['wire_a', 'drawing'],
        'net_intent': 'VSS',
        'clearance': 1,
    }
    params.update(overrides)
    return execute(editor, 'create_pour', params)


def geometry(editor, uids):
    objects = projected(editor.w)
    return unary_union([Polygon(objects[uid].shape.points) for uid in uids])


def assert_orthogonal(editor, uids):
    objects = projected(editor.w)
    for uid in uids:
        points = objects[uid].shape.points
        assert len(points) >= 3
        assert all(a[0] == b[0] or a[1] == b[1] for a, b in zip(points, points[1:] + points[:1]))


def test_empty_pour_is_one_net_polygon_and_one_undo(editor):
    before = editor.w
    result = pour(editor)
    assert result['polygon_count'] == 1
    assert result['area_um2'] == pytest.approx(100)
    shape = projected(editor.w)[result['created_uids'][0]].shape
    assert shape.net_intent == 'VSS'
    assert shape.lpp == ('wire_a', 'drawing')
    assert geometry(editor, result['created_uids']).equals(box(0, 0, 20000, 20000))
    editor.undo()
    assert editor.w.operations == before.operations


def test_only_explicit_target_is_connected_and_other_islands_are_removed(editor):
    target = editor.create(Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((0, 4000), (4000, 12000)), net_intent='VSS'))
    obstacle = editor.create(Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((8000, 0), (12000, 20000)), net_intent='VDD'))
    before = editor.w
    result = pour(editor, targets=[target])
    copper = geometry(editor, result['created_uids'])
    assert copper.intersection(box(0, 4000, 4000, 12000)).area > 0
    assert copper.distance(box(8000, 0, 12000, 20000)) >= 2000 - 1e-6
    assert copper.bounds[2] < 8000
    assert result['connected_target_uids'] == [target]
    assert result['discarded_island_count'] == 1
    assert all(v % editor.profile.grid == 0 for uid in result['created_uids'] for point in projected(editor.w)[uid].shape.points for v in point)
    editor.undo()
    assert editor.w.operations == before.operations
    assert obstacle in projected(editor.w)


def test_holed_obstacle_produces_syncable_orthogonal_polygons_with_clearance(editor):
    editor.create(Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((8000, 8000), (12000, 12000)), net_intent='VDD'))
    result = pour(editor)
    assert result['polygon_count'] > 1
    assert result['rectangle_slice_count'] >= result['polygon_count']
    assert result['orthogonal'] is True
    copper = geometry(editor, result['created_uids'])
    assert copper.area / editor.profile.dbu_per_micron**2 == pytest.approx(result['area_um2'])
    assert copper.distance(box(8000, 8000, 12000, 12000)) >= 2000 - 1e-6
    expected = box(0, 0, 20000, 20000).difference(box(8000, 8000, 12000, 12000).buffer(2000 + 2**0.5, join_style='mitre'))
    assert copper.equals(set_precision(expected, grid_size=editor.profile.grid, mode='valid_output'))
    assert_orthogonal(editor, result['created_uids'])


def test_l_shaped_pour_is_merged_into_one_concave_polygon(editor):
    editor.create(Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((10000, 10000), (20000, 20000)), net_intent='VDD'))
    result = pour(editor)
    assert result['polygon_count'] == 1
    assert result['rectangle_slice_count'] > 1
    shape = projected(editor.w)[result['created_uids'][0]].shape
    assert len(shape.points) == 6
    assert_orthogonal(editor, result['created_uids'])


def test_u_shaped_pour_is_merged_into_one_concave_polygon(editor):
    editor.create(Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((8000, 12000), (12000, 20000)), net_intent='VDD'))
    result = pour(editor)
    assert result['polygon_count'] == 1
    assert result['rectangle_slice_count'] > 1
    shape = projected(editor.w)[result['created_uids'][0]].shape
    assert len(shape.points) == 8
    assert_orthogonal(editor, result['created_uids'])


def test_diagonal_obstacle_remains_on_grid_without_rounding_away_clearance(editor):
    editor.create(Shape(kind='polygon', lpp=('wire_a', 'drawing'), points=((10000, 6000), (14000, 10000), (10000, 14000), (6000, 10000)), net_intent='VDD'))
    result = pour(editor)
    copper = geometry(editor, result['created_uids'])
    obstacle = Polygon(((10000, 6000), (14000, 10000), (10000, 14000), (6000, 10000)))
    assert copper.distance(obstacle) >= 2000 - 1e-6
    assert all(v % editor.profile.grid == 0 for uid in result['created_uids'] for point in projected(editor.w)[uid].shape.points for v in point)
    assert_orthogonal(editor, result['created_uids'])


def test_conflicting_target_and_missing_target_layer_are_atomic(editor):
    target = editor.create(Shape(kind='rect', lpp=('wire_b', 'drawing'), bbox=((0, 0), (4000, 4000)), net_intent='VDD'))
    other = editor.create(Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((0, 0), (4000, 4000)), net_intent='VSS'))
    before = editor.w.model_dump_json()
    with pytest.raises(LiteError, match='POUR_TARGET_NET_CONFLICT'):
        pour(editor, targets=[target])
    assert editor.w.model_dump_json() == before
    with pytest.raises(LiteError, match='POUR_TARGET_LAYER_MISSING'):
        pour(editor, lpp=['wire_b', 'drawing'], targets=[other])
    assert editor.w.model_dump_json() == before


def test_instance_cannot_expose_all_internal_metal_as_a_target(editor):
    before = editor.w.model_dump_json()
    with pytest.raises(LiteError, match='POUR_TARGET_TYPE_UNSUPPORTED'):
        pour(editor, targets=['inst'])
    assert editor.w.model_dump_json() == before


def test_configured_spacing_is_a_lower_bound(editor, profile):
    rules = DrcProfile(
        id='rules',
        technology_fingerprint=profile.native_fingerprint,
        rules=(Rule(id='M1.SP', kind='spacing', layer='wire_a', value=1.5),),
    )
    before = editor.w.model_dump_json()
    with pytest.raises(LiteError, match='POUR_CLEARANCE_BELOW_RULE'):
        execute(editor, 'create_pour', {
            'bounds_um': [0, 0, 10, 10],
            'lpp': ['wire_a', 'drawing'],
            'net_intent': 'VSS',
            'clearance': 1,
        }, rules=rules)
    assert editor.w.model_dump_json() == before


def test_unverified_explicit_target_is_reported_not_relabelled(editor):
    target = editor.create(Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((0, 0), (4000, 4000))))
    result = pour(editor, targets=[target])
    assert result['unverified_target_uids'] == [target]
    assert projected(editor.w)[target].shape.net_intent is None


def test_agent_discovers_and_adopts_pour_through_the_real_job_protocol(editor, tmp_path):
    from colayout_lite.agent_tools.session import AgentSession
    from colayout_lite.regions.models import OperationRegion

    editor.w = editor.w.model_copy(update={'baseline': editor.w.baseline.model_copy(update={'objects': {}})})
    target = editor.create(Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((0, 0), (4000, 4000)), net_intent='VSS'))
    region_id = 'pour'
    region = OperationRegion(id=region_id, name='pour', bbox=((0, 0), (20000, 20000)), object_uids=(target,), blocked_layers=('wire_a', 'wire_b'))
    editor.w = editor.w.model_copy(update={'local_data': editor.w.local_data.model_copy(update={'regions': {region_id: region}})})
    session = AgentSession(editor, None, tmp_path / 'agent')
    try:
        def call(tool, args):
            return session.invoke(tool, args, client_id='layout')

        token = call('claim_region', {'region_id': region_id, 'agent_name': 'layout'})['result']['session_token']
        actions = call('tools', {'tool': 'edit_region'})['result']['tools'][0]['usage']['actions']
        assert 'create_pour' in actions
        submitted = call('edit_region', {
            'session_token': token,
            'mode': 'apply',
            'expected_revision': editor.w.revision,
            'operations': [{
                'action': 'create_pour',
                'params': {
                    'bounds_um': [0, 0, 10, 10],
                    'lpp': ['wire_a', 'drawing'],
                    'net_intent': 'VSS',
                    'clearance': 1,
                    'targets': [target],
                },
            }],
        })['result']
        assert submitted['edit_applied'] is False
        job_id = submitted['job_id']
        session.jobs[job_id].future.result(timeout=10)
        status = call('edit_region', {'session_token': token, 'mode': 'status', 'job_id': job_id})['result']
        assert status['state'] == 'completed' and status['edit_applied'] is True
        assert status['result']['created_uids']
        assert set(status['result']['created_uids']) <= set(projected(editor.w))
    finally:
        session.close()


def test_qt_button_and_canvas_drag_generate_pour(editor):
    from types import SimpleNamespace

    from PySide6.QtCore import QPointF, QRectF, Qt, Signal
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel, QGraphicsScene, QMainWindow

    from colayout_lite.presentation.canvas import Canvas
    from colayout_lite.pour_panel import show
    from colayout_lite.rectangle_panel import show as show_rectangle

    class Selection:
        def __init__(self):
            self.value = []

        def ids(self):
            return list(self.value)

        def set_ids(self, value):
            self.value = list(value)

        def escape(self):
            pass

        def cancel(self):
            pass

    class Window(QMainWindow):
        view_state_changed = Signal()

    app = QApplication.instance() or QApplication([])
    window = Window()
    selection = Selection()
    window.editor = editor
    window.busy = False
    window.human = selection
    window.selection_ui = selection
    window.drc = SimpleNamespace(profile=None, pick=SimpleNamespace(setChecked=lambda _: None))
    window.status = QLabel()
    window.canvas = Canvas(QGraphicsScene(window))
    window.setCentralWidget(window.canvas)
    target = editor.create(Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((0, 0), (4000, 4000)), net_intent='VSS'))
    before = editor.w
    selection.set_ids([target])
    window.resize(900, 700)
    window.show()
    try:
        show_rectangle(window)
        show(window)
        panel = window.pour_panel
        rectangle_panel = window.rectangle_panel
        assert [rectangle_panel.layers.itemText(i) for i in range(rectangle_panel.layers.count())] == [panel.layers.itemText(i) for i in range(panel.layers.count())]
        assert [rectangle_panel.layers.itemData(i) for i in range(rectangle_panel.layers.count())] == [panel.layers.itemData(i) for i in range(panel.layers.count())]
        assert rectangle_panel.layers.currentData() == panel.layers.currentData()
        QTest.keyClicks(panel.net, 'VSS')
        QTest.mouseClick(panel.draw, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert panel.context is not None and window.canvas.region_select
        window.canvas.setSceneRect(QRectF(0, -20000, 20000, 20000))
        window.canvas.fitInView(QRectF(0, -20000, 20000, 20000))
        app.processEvents()
        start = window.canvas.mapFromScene(QPointF(0, 0))
        end = window.canvas.mapFromScene(QPointF(10000, -10000))
        QTest.mousePress(window.canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(window.canvas.viewport(), pos=end)
        QTest.mouseRelease(window.canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
        app.processEvents()
        assert panel.context is None and not window.canvas.region_select
        assert '已生成 1 个多边形' in panel.message.text()
        created = set(projected(editor.w)) - set(projected(before))
        assert len(created) == 1
        assert selection.ids() == list(created)
        editor.undo()
        assert editor.w.operations == before.operations
    finally:
        window.close()
        app.processEvents()


def test_rectangle_toolbar_menu_contains_parallel_rectangle_and_pour_actions():
    from types import MethodType, SimpleNamespace

    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication, QToolBar, QToolButton

    from colayout_lite.studio_shell import StudioShell

    app = QApplication.instance() or QApplication([])
    toolbar = QToolBar()
    rectangle = QAction('矩形')
    pour_action = QAction('铺铜')
    shell = SimpleNamespace(
        toolbar=toolbar,
        menus={},
        action_buttons={},
        commands=SimpleNamespace(actions={'rectangle': rectangle, 'pour': pour_action}),
    )
    shell.menu_button = MethodType(StudioShell.menu_button, shell)
    shell.add_actions = MethodType(StudioShell.add_actions, shell)
    menu = StudioShell.rectangle_menu(shell)
    button = menu.parentWidget()
    try:
        assert menu.objectName() == 'compactMenu_rectangle'
        assert menu.actions() == [rectangle, pour_action]
        assert button.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup
        assert shell.action_buttons['rectangle'] == [button]
        assert shell.action_buttons['pour'] == [button]
    finally:
        menu.hide()
        toolbar.close()
        app.processEvents()
