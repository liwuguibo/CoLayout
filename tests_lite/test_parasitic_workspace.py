# ruff: noqa: F811
from shapely.geometry import box

from colayout_lite.circuit.models import LayoutConnectivity, LayoutInstance, Terminal, ViewRef
from colayout_lite.model import Object, Placement, Shape, Snapshot, Workspace
from colayout_lite.parasitic_workspace import analysis_graph, analyze_workspace
from tests_lite.test_parasitic_rc import profile, rc


def blackbox_workspace(profile):
    node = [None] * 12
    node[10] = []
    node[11] = [['child_count', 1]]
    root = [None] * 12
    root[4] = 1
    root[5] = [[0, 0], [20000, 6000]]
    root[9] = []
    root[10] = []
    root[11] = []
    instance = Object(uid='bb', native_id='bb', kind='inst', placement=Placement(origin=(0, 0)),
        bbox=((0, 4000), (20000, 6000)), attributes={'master_key': 'bb', 'name': 'BB'})
    wire = Object(uid='wire', native_id='wire', kind='path', shape=Shape(
        kind='path', lpp=('wire_a', 'drawing'), points=((0, 0), (20000, 0)), width=2000))
    pin = dict(instance_uid='bb', terminal='IN', kind='rect', lpp=['wire_a', 'drawing'], pin_index=0, figure_index=0,
        local_bbox=((0, 4000), (20000, 6000)), transform=(1, 0, 0, 1, 0, 0),
        local_geometry=dict(kind='rect', bbox=((0, 4000), (20000, 6000))))
    snapshot = Snapshot(library='L', cell='C', library_path='/test/L', session='s', counter='1', dirty=False,
        dbu_per_micron=2000, grid=2, objects={'bb': instance, 'wire': wire}, pins=[pin],
        display={'root': 'root', 'nodes': {'root': root, 'bb': node}, 'styles': []}, technology_fingerprint='fp')
    native = LayoutInstance(uid='bb', name='BB', master=('L', 'bb', 'layout'), black_box=True,
        terminals={'IN': Terminal(name='IN', net='B')})
    connectivity = LayoutConnectivity(source=ViewRef(library='L', cell='C', view='layout', session='s', counter='1'),
        instances={'bb': native}, ports={})
    return Workspace(baseline=snapshot, profile_id=profile.id, layout_connectivity=connectivity)


def guard_blackbox(monkeypatch):
    def guarded(snapshot, *, owner_filter=None, **kwargs):
        assert not owner_filter or 'bb' not in owner_filter
        return {}, {}
    monkeypatch.setattr('colayout_lite.scene.figures_by_owner', guarded)


def test_blackbox_final_geometry_contains_only_public_pin(profile, monkeypatch):
    guard_blackbox(monkeypatch)
    graph = analysis_graph(blackbox_workspace(profile), profile, None)
    assert graph['blackbox_ids'] == {'bb'}
    rows = [node for node in graph['nodes'] if node['owner_uid'] == 'bb']
    assert len(rows) == 1
    assert rows[0]['geometry'].equals(box(0, 4000, 20000, 6000))
    assert rows[0]['component_id'] in graph['component_pins']


def test_workspace_estimate_uses_public_pin_as_neighbor(profile, monkeypatch):
    guard_blackbox(monkeypatch)
    result, graph = analyze_workspace(blackbox_workspace(profile), profile, None, ['wire'])
    assert result.resistance_ohm == 1
    assert result.ground_capacitance_fF == 31
    assert result.coupling_capacitance_fF > 0
    assert result.impacts[0].neighbor_sources[0].startswith('bb:')
    assert result.impacts[0].relation == 'unknown_net'
