"""Native dummy exceptions retain physical facts, never invent circuit bindings."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from colayout_lite.adapter import Adapter
from colayout_lite.circuit.codec import decode_context
from colayout_lite.circuit.query import layout_intent, require_context
from colayout_lite.document import load_document, save_document
from colayout_lite.model import LiteError, Object, Snapshot
from colayout_lite.technology import TechnologyProfile


def fixture():
    profile = TechnologyProfile(id='fixture', label='Fixture', native_fingerprint='fp',
                                dbu_per_micron=1000, grid=1, writable_lpps=(), layers=[], vias={})
    props = [['lxDummy', 'boolean', ['string', 'TRUE']],
             ['lxDummyOwner', 'string', ['string', 'M0']],
             ['lxDummyNet', 'string', ['string', 'TIE']]]
    objects = {}
    for uid, name, properties in [('device', 'M0', []), ('dummy', 'FILLER', props)]:
        objects[uid] = Object(uid=uid, native_id=uid, kind='inst',
            bbox=((0, 0), (100, 100)), attributes={'name': name},
            native_row=[uid, 'inst', [['0', '0'], ['100', '100']], None, None,
                        properties, [['name', name]]])
    snapshot = Snapshot(library='L', cell='C', library_path='/project/L',
        session='session', counter='1', dirty=False, dbu_per_micron=1000, grid=1,
        technology_fingerprint='fp', objects=objects)
    terms = [[name, 'TIE', 'inputOutput', [[name, 'TIE']], [[name, 'TIE']]]
             for name in ('D', 'G', 'S', 'B')]
    path = [['L', 'C', 'schematic', 'M0', 0, 0, 0]]
    raw = ['colayout-context-native-5', ['L', 'C', 'schematic', 'session', '2'],
           [['M0', ['fixture', 'mos', 'symbol'], terms, ['no_cdf', []], [], 1]], [],
           [['device', 'M0', ['fixture', 'mos', 'layout'], '0', terms, [path]],
            ['dummy', 'FILLER', ['fixture', 'mos', 'layout'], '0', deepcopy(terms), None]],
           [], [True, '*** Checking layout cellview fixture\n'
                      '*** Check Against Source complete. ***\n', True], 'pair', []]
    return raw, snapshot, profile


@pytest.mark.parametrize('value', [['string', 'TRUE'], ['opaque', 't']])
def test_native_dummy_keeps_geometry_terminals_and_no_fabricated_binding(value):
    raw, snapshot, _ = fixture()
    snapshot.objects['dummy'].native_row[5][0][2] = value
    original = snapshot.model_dump(mode='json')
    circuit, layout = decode_context(raw, snapshot)
    assert snapshot.model_dump(mode='json') == original
    assert set(layout.instances) == {'device', 'dummy'}
    assert set(layout.instances['dummy'].terminals) == {'D', 'G', 'S', 'B'}
    assert {binding.layout_uid for binding in circuit.bindings} == {'device'}
    assert set(circuit.instances) == {'M0'}
    assert 'FILLER.G' in layout_intent(layout)['nets']['TIE']


@pytest.mark.parametrize('props', [[], [['lxDummy', 'boolean', None]],
    [['lxDummy', 'boolean', ['string', 'FALSE']]],
    [['lxDummy', 'string', ['string', 'TRUE']]],
    [['ignore', 'boolean', ['string', 'TRUE']]],
    [['lxDummy', 'boolean', ['number', '1']]],
    [['lxDummy', 'boolean', ['string', 'TRUE']]] * 2])
def test_name_or_untrusted_property_never_exempts_unbound_device(props):
    raw, snapshot, _ = fixture()
    raw[4][1][1] = 'Dummy_M0'
    snapshot.objects['dummy'].native_row[5] = props
    with pytest.raises(LiteError, match='NATIVE_BINDING_MISSING'):
        decode_context(raw, snapshot)


@pytest.mark.parametrize('case', ['signal', 'missing_net', 'member_net', 'no_terms',
                                 'hierarchy', 'empty_path', 'partial_path'])
def test_dummy_exception_requires_leaf_tied_terminals_and_absent_not_broken_paths(case):
    raw, snapshot, _ = fixture()
    row = raw[4][1]
    if case == 'signal':
        row[4][1] = ['G', 'SIGNAL', 'input', [['G', 'SIGNAL']], [['G', 'SIGNAL']]]
    elif case == 'missing_net':
        row[4][1] = ['G', None, 'input', [['G', None]], []]
    elif case == 'member_net':
        row[4][1][3] = [['G', 'DISTINCT_NATIVE_NET']]
    elif case == 'no_terms':
        row[4] = []
    elif case == 'hierarchy':
        row[3] = '1'
    elif case == 'empty_path':
        row[5] = [[]]
    else:
        row[5] = [raw[4][0][5][0], []]
    with pytest.raises(LiteError, match='NATIVE_BINDING_MISSING'):
        decode_context(raw, snapshot)


def test_existing_dummy_binding_is_preserved_and_wrong_source_is_rejected():
    raw, snapshot, _ = fixture()
    raw[4][1][5] = deepcopy(raw[4][0][5])
    raw[4][1][4][1] = ['G', 'SIGNAL', 'input', [['G', 'SIGNAL']], [['G', 'SIGNAL']]]
    circuit, _ = decode_context(raw, snapshot)
    assert len(circuit.bindings) == 2
    assert circuit.bindings[1].schematic_paths == circuit.bindings[0].schematic_paths
    raw[4][1][5][0][0][0] = 'another_library'
    with pytest.raises(LiteError, match='NATIVE_CONTEXT_INVALID'):
        decode_context(raw, snapshot)


@pytest.mark.parametrize('case,code', [('diagnostic', 'NATIVE_CORRESPONDENCE_REJECTED'),
    ('changed', 'SOURCE_CHANGED_DURING_READ'), ('missing_instance', 'NATIVE_CONTEXT_INVALID')])
def test_native_checks_remain_required(case, code):
    raw, snapshot, _ = fixture()
    if case == 'diagnostic':
        raw[6][1] = raw[6][1].replace('*** Check Against', 'Unbound ordinary device\n*** Check Against')
    elif case == 'changed':
        raw[6][2] = False
    else:
        raw[4].pop()
    with pytest.raises(LiteError, match=code):
        decode_context(raw, snapshot)


def test_unplaced_schematic_instance_lx1001_is_retained_without_layout_target():
    raw, snapshot, _ = fixture()
    raw[2].append([
        'C_DECAP',
        ['pdk', 'crtmom_rf', 'symbol'],
        [['PLUS', 'VDD', 'inputOutput', [['PLUS', 'VDD']], [['PLUS', 'VDD']]],
         ['MINUS', 'VSS', 'inputOutput', [['MINUS', 'VSS']], [['MINUS', 'VSS']]]],
        ['captured', []],
        [],
        1,
    ])
    lx1001 = "INFO (LX-1001): Schematic instance 'C_DECAP' has no corresponding layout instance."
    raw[6][1] = raw[6][1].replace('*** Check Against', lx1001 + '\n*** Check Against')
    circuit, connectivity = decode_context(raw, snapshot)
    assert 'C_DECAP' in circuit.instances
    assert lx1001 in circuit.native_check.report
    assert 'C_DECAP' not in {inst.name for inst in connectivity.instances.values()}


def test_adapter_import_roundtrip_refresh_and_rejection_preserve_dummy(tmp_path, monkeypatch):
    raw, snapshot, profile = fixture()
    calls = []
    native = SimpleNamespace(state=tmp_path, config_path=tmp_path/'connection.json',
        call=lambda *args: calls.append(args),
        read_pair=lambda *args: ['colayout-paired-native-3', None, raw])
    adapter = Adapter(native)
    adapter.last_read_timings = {}
    monkeypatch.setattr(adapter, '_decode_layout', lambda *args: (snapshot, profile))
    workspace, _ = adapter.open_workspace('L', 'C')
    path = tmp_path/'document.json'
    save_document(path, workspace, profile)
    restored, _ = load_document(path)
    require_context(restored)
    assert restored.baseline.objects == workspace.baseline.objects
    assert restored.layout_connectivity == workspace.layout_connectivity
    assert restored.circuit == workspace.circuit
    refreshed, _ = adapter.refresh(restored)
    assert refreshed.layout_connectivity.instances['dummy'] == restored.layout_connectivity.instances['dummy']
    assert refreshed.circuit.bindings == restored.circuit.bindings
    before = path.read_bytes()
    raw[4][0][5] = None  # Ordinary device loses its native binding.
    with pytest.raises(LiteError, match='NATIVE_BINDING_MISSING'):
        adapter.refresh(restored)
    assert path.read_bytes() == before
    assert calls and all(call[0] == 'ltPrepare' for call in calls)


def test_refresh_prunes_deleted_region_member_and_keeps_group_references_strict(tmp_path, monkeypatch):
    from colayout_lite.local_models import LocalGroup
    from colayout_lite.model import Shape
    from colayout_lite.regions.models import OperationRegion

    raw, snapshot, profile = fixture()
    wire = Object(
        uid='wire1',
        native_id='wire1',
        kind='rect',
        bbox=((10, 10), (50, 50)),
        shape=Shape(kind='rect', lpp=('M1', 'drawing'), bbox=((10, 10), (50, 50)), net_intent='TIE'),
    )
    snap_with_wire = snapshot.model_copy(update={'objects': {**snapshot.objects, 'wire1': wire}})
    current_snap = snap_with_wire
    native = SimpleNamespace(
        state=tmp_path,
        config_path=tmp_path / 'connection.json',
        call=lambda *args: None,
        read_pair=lambda *args: ['colayout-paired-native-3', None, raw],
    )
    adapter = Adapter(native)
    adapter.last_read_timings = {}
    monkeypatch.setattr(adapter, '_decode_layout', lambda *args: (current_snap, profile))

    workspace, _ = adapter.open_workspace('L', 'C')
    reg = OperationRegion(
        id='area-1',
        bbox=((-100, -100), (200, 200)),
        name='Area 1',
        object_uids=('device', 'dummy', 'wire1'),
    )
    workspace = workspace.model_copy(
        update={'local_data': workspace.local_data.model_copy(update={'regions': {'area-1': reg}})}
    )

    # Server deletes wire1 inside region: refresh prunes wire1 from region.object_uids.
    current_snap = snapshot
    refreshed, _ = adapter.refresh(workspace)
    assert refreshed.local_data.regions['area-1'].object_uids == ('device', 'dummy')

    # LocalGroup referencing a server-deleted object still raises LOCAL_REFERENCES_MISSING.
    grouped = workspace.model_copy(
        update={
            'local_data': workspace.local_data.model_copy(
                update={'groups': {'g1': LocalGroup(id='g1', name='G1', object_uids=('wire1',))}}
            )
        }
    )
    with pytest.raises(LiteError, match='LOCAL_REFERENCES_MISSING'):
        adapter.refresh(grouped)
