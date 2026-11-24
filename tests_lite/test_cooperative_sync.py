"""Behavioral and Qt UI tests for decoupled Virtuoso <-> CoLayout cooperative editing."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from colayout_lite.adapter import Adapter
from colayout_lite.agent_tools.app import enable as enable_agent_hub
from colayout_lite.agent_tools.session import AgentSession
from colayout_lite.circuit.models import (
    CircuitContext,
    LayoutConnectivity,
    LayoutInstance,
    NativeBinding,
    NativeCheck,
    NativePathSegment,
    ReferenceInstance,
    Terminal,
    ViewRef,
)
from colayout_lite.cooperative_sync import (
    ascii_guide_label,
    build_region_guides,
)
from colayout_lite.document import save_document
from colayout_lite.editor import Editor, intent_properties, projected
from colayout_lite.model import (
    LiteError,
    MultipartPath,
    NativeRoute,
    Object,
    Placement,
    Shape,
    Snapshot,
    Workspace,
)
from colayout_lite.regions.actions import RegionActions
from colayout_lite.studio_window import StudioWindow
from colayout_lite.technology import TechnologyProfile
from colayout_lite.workspace_files import WorkspaceFiles
from colayout_lite.workspace_storage import WorkspaceStorage


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _coop_profile() -> TechnologyProfile:
    return TechnologyProfile(
        id='coop-tech',
        label='Coop Tech',
        native_fingerprint='fp-coop',
        dbu_per_micron=1000,
        grid=5,
        writable_lpps=(('M1', 'drawing'), ('M2', 'drawing')),
        layers=[
            {'layer': 'M1', 'purpose': 'drawing', 'material': 'metal'},
            {'layer': 'M2', 'purpose': 'drawing', 'material': 'metal'},
        ],
        vias={
            'VIA1': {
                'kind': 'stdViaDef',
                'layer1': 'M1',
                'layer2': 'M2',
                'cut_layer': 'V1',
                'implant1': None,
                'implant2': None,
                'defaults': {
                    'cutWidth': 100,
                    'cutHeight': 100,
                    'cutRows': 1,
                    'cutColumns': 1,
                    'cutSpacing': [100, 100],
                    'layer1Enc': [50, 50],
                    'layer2Enc': [50, 50],
                    'layer1Offset': [0, 0],
                    'layer2Offset': [0, 0],
                    'originOffset': [0, 0],
                },
            }
        },
    )


def _rect_obj(
    uid: str,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    *,
    lpp=('M1', 'drawing'),
    net: str | None = None,
    name: str | None = None,
    missing_uid: bool = False,
) -> Object:
    shape = Shape(kind='rect', lpp=lpp, bbox=((x0, y0), (x1, y1)), net_intent=net)
    attrs = {'name': name} if name else {}
    props = [['colayoutNetIntent', 'string', ['string', net or '']]]
    if not missing_uid:
        props.append(['colayoutObjectId', 'string', ['string', uid]])
    return Object(
        uid=uid,
        native_id=f'nat:{uid}',
        kind='rect',
        bbox=((x0, y0), (x1, y1)),
        shape=shape,
        attributes=attrs,
        properties=props,
        observed_net=net,
        read_only_reason='missing_persistent_identity' if missing_uid else None,
    )


def _initial_snapshot_and_context(prof: TechnologyProfile):
    objects = {}
    physical = {}
    logical = {}
    bindings = []
    # area-1 around M_A (0..5000, 0..5000); area-2 around M_B (12000..17000, 0..5000)
    # external objects: ext_keep (22..24 um), ext_edit (25..27 um), ext_del (28..30 um), ext_aux (34..36 um)
    for name, x in [('M_A', 1000), ('M_B', 13000)]:
        uid = f'inst-{name}'
        net = f'NET_{name}'
        terms = {'S': Terminal(name='S', net=net, members={'S': net})}
        objects[uid] = Object(
            uid=uid,
            native_id=f'nat-{name}',
            kind='inst',
            bbox=((x, 1000), (x + 2000, 3000)),
            placement=Placement(origin=(x, 1000)),
            attributes={'name': name, 'master': ['LIB', 'nmos', 'layout'], 'master_key': 'nmos'},
            properties=[['colayoutObjectId', 'string', ['string', uid]]],
        )
        physical[uid] = LayoutInstance(
            uid=uid, name=name, master=('LIB', 'nmos', 'layout'), terminals=terms, black_box=False
        )
        logical[name] = ReferenceInstance(name=name, master=('LIB', 'nmos', 'symbol'), terminals=terms)
        bindings.append(
            NativeBinding(
                layout_uid=uid,
                schematic_paths=(
                    (
                        NativePathSegment(
                            library='LIB',
                            cell='TOP',
                            view='schematic',
                            instance=name,
                            member=0,
                            row=0,
                            column=0,
                        ),
                    ),
                ),
            )
        )
    objects['ext_keep'] = _rect_obj('ext_keep', 22000, 0, 24000, 1000, net='EXT1', name='EXT_KEEP')
    objects['ext_edit'] = _rect_obj('ext_edit', 25000, 0, 27000, 1000, net='EXT2', name='EXT_EDIT')
    objects['ext_del'] = _rect_obj('ext_del', 28000, 0, 30000, 1000, net='EXT3', name='EXT_DEL')
    objects['ext_aux'] = _rect_obj('ext_aux', 34000, 1000, 36000, 2000, net='EXT4', name='EXT_AUX')

    source = ViewRef(library='LIB', cell='TOP', view='schematic', session='s-coop', counter='1')
    circuit = CircuitContext(
        source=source,
        pair_id='pair-coop',
        instances=logical,
        ports={
            'P_A': Terminal(name='P_A', net='NET_M_A'),
            'P_B': Terminal(name='P_B', net='NET_M_B'),
        },
        bindings=tuple(bindings),
        native_check=NativeCheck(report='fixture ok'),
    )
    connectivity = LayoutConnectivity(
        source=source.model_copy(update={'view': 'layout'}),
        pair_id='pair-coop',
        instances=physical,
        ports={},
    )
    display = {
        'root': 'root',
        'nodes': {
            'root': ['root', 'LIB', 'TOP', 'layout', '1000', None, [], [], [], [], []],
            'nmos': ['nmos', 'LIB', 'nmos', 'layout', '1000', None, [], [], [], [], []],
        },
        'styles': [],
        'unsupported': [],
    }
    snap = Snapshot(
        library='LIB',
        cell='TOP',
        library_path='/remote/proj/LIB',
        session='s-coop',
        counter='1',
        dirty=False,
        dbu_per_micron=prof.dbu_per_micron,
        grid=prof.grid,
        objects=objects,
        routes={},
        fig_groups={},
        display=display,
        technology_fingerprint=prof.native_fingerprint,
    )
    return snap, circuit, connectivity


class FakeCoopNative:
    """In-memory Virtuoso mock supporting ltPrepare, ltSyncRegionGuides, read_pair and transactions."""

    def __init__(self, tmp_path, snapshot: Snapshot, prof: TechnologyProfile, circuit, connectivity):
        self.state = tmp_path / 'native-state'
        self.state.mkdir(parents=True, exist_ok=True)
        self.config_path = tmp_path / 'connection.json'
        self.config_path.write_text(
            '{"bridge_env":"test.env","state_directory":"native-state","allowed_remote_root":"/remote/proj"}',
            encoding='utf8',
        )
        self.config = {'allowed_remote_root': '/remote/proj'}
        self.requires_circuit_context = True
        self.current = snapshot
        self.profile = prof
        self.circuit = circuit
        self.connectivity = connectivity
        self.receipts = {}
        self.calls = []
        self.guides = []
        self.submissions = 0
        self.behavior = 'saved'
        self._prep_seq = 0

    def read(self, library, cell):
        assert (library, cell) == (self.current.library, self.current.cell)
        return self.current, self.profile

    def call(self, name, *values):
        self.calls.append((name, values))
        if name == 'ltReceiptRead':
            return self.receipts.get(values[0])
        if name == 'ltRelease':
            return [1, 1, 0]
        if name == 'ltSyncRegionGuides':
            lib, cell, guides = values
            if cell.startswith('COLAYOUT_MVP_LITE_'):
                raise AssertionError(f'ltSyncRegionGuides must not be called on temporary cell {cell}')
            assert (lib, cell) == (self.current.library, self.current.cell)
            self.guides = deepcopy(guides)
            return ['synced', len(guides), self.current.counter]
        if name == 'ltPrepare':
            if self.current.dirty:
                raise LiteError('NATIVE_ERROR', 'ltPrepare: ("VIRTUOSO_UNSAVED_CHANGES")')
            new_objects = {}
            remapped = False
            for uid, obj in self.current.objects.items():
                if uid.startswith('session:') or obj.read_only_reason in (
                    'missing_persistent_identity',
                    'duplicate_persistent_identity',
                ):
                    self._prep_seq += 1
                    new_uid = f'prep:{self._prep_seq}'
                    props = [p for p in obj.properties if p[0] != 'colayoutObjectId'] + [
                        ['colayoutObjectId', 'string', ['string', new_uid]]
                    ]
                    new_objects[new_uid] = obj.model_copy(
                        update={'uid': new_uid, 'properties': props, 'read_only_reason': None}
                    )
                    remapped = True
                else:
                    new_objects[uid] = obj
            if remapped:
                next_c = str(int(self.current.counter) + 1)
                self.current = self.current.model_copy(
                    update={'objects': new_objects, 'counter': next_c}
                )
            return [True]
        raise AssertionError(f'Unexpected native call: {name}')

    def transact(self, library, cell, request):
        assert (library, cell) == (self.current.library, self.current.cell)
        self.submissions += 1
        if request['counter'] != self.current.counter:
            raise LiteError('NATIVE_ERROR', 'SOURCE_CONFLICT')
        if self.current.dirty:
            raise LiteError('NATIVE_ERROR', 'VIRTUOSO_UNSAVED_CHANGES')
        if self.behavior == 'lost_before_apply':
            raise TimeoutError('lost before apply')
        status = 'rolled_back' if self.behavior == 'rolled_back' else 'saved'
        if status == 'saved':
            objs = dict(self.current.objects)
            for cmd in request['operations']:
                act = cmd['action']
                uid = cmd['uid']
                if act in ('create', 'update'):
                    sd = dict(cmd['shape'])
                    if 'bbox' in sd:
                        sd['bbox'] = [[int(v) for v in pt] for pt in sd['bbox']]
                    if 'points' in sd:
                        sd['points'] = [[int(v) for v in pt] for pt in sd['points']]
                    if 'width' in sd:
                        sd['width'] = int(sd['width'])
                    s = Shape.model_validate(sd)
                    if act == 'create':
                        objs[uid] = Object(
                            uid=uid,
                            native_id=f'nat:{uid}',
                            kind=s.kind,
                            bbox=s.bbox,
                            shape=s,
                            properties=[
                                ['colayoutObjectId', 'string', ['string', uid]],
                                ['colayoutNetIntent', 'string', ['string', s.net_intent or '']],
                            ],
                            observed_net=s.net_intent,
                        )
                    else:
                        objs[uid] = objs[uid].model_copy(
                            update={
                                'shape': s,
                                'bbox': s.bbox,
                                'properties': intent_properties(objs[uid].properties, s.net_intent),
                                'observed_net': s.net_intent,
                            }
                        )
                elif act == 'delete':
                    objs.pop(uid)
                elif act == 'transform':
                    pl_data = dict(cmd['placement'])
                    pl_data['origin'] = [int(v) for v in pl_data['origin']]
                    pl = Placement.model_validate(pl_data)
                    objs[uid] = objs[uid].model_copy(update={'placement': pl})
            next_c = str(int(self.current.counter) + 1)
            self.current = self.current.model_copy(update={'objects': objs, 'counter': next_c})
        receipt = [
            'colayout-lite-receipt-1',
            request['request_id'],
            request['hash'],
            status,
            library,
            cell,
            request['backup'],
            ['done', len(request['operations'])],
        ]
        self.receipts[request['request_id']] = receipt
        if self.behavior == 'lost_after_apply':
            raise TimeoutError('lost after apply')
        return receipt


def _make_adapter_rig(tmp_path):
    prof = _coop_profile()
    snap, circuit, connectivity = _initial_snapshot_and_context(prof)
    fake = FakeCoopNative(tmp_path, snap, prof, circuit, connectivity)
    adapter = Adapter(fake)
    adapter.read = fake.read

    def fake_read_pair(lib, cell, *, schematic=None):
        s, p = fake.read(lib, cell)
        c = fake.circuit.model_copy(
            update={
                'source': fake.circuit.source.model_copy(
                    update={'library': lib, 'cell': cell, 'counter': s.counter}
                )
            }
        )
        conn = fake.connectivity.model_copy(
            update={
                'source': fake.connectivity.source.model_copy(
                    update={'library': lib, 'cell': cell, 'counter': s.counter}
                )
            }
        )
        return s, p, c, conn

    adapter.read_pair = fake_read_pair
    fake.read_pair = fake_read_pair
    editor = Editor(
        Workspace(
            baseline=snap,
            profile_id=prof.id,
            circuit=circuit,
            layout_connectivity=connectivity,
            connection_config=str(fake.config_path),
        ),
        prof,
    )
    a1 = RegionActions(editor).create((0, 0, 5000, 5000), 'Core_A')
    a2 = RegionActions(editor).create((12000, 0, 17000, 5000), '模拟区二')
    return adapter, fake, editor, a1, a2


def _wait_qt_until(predicate, *, timeout_ms: int = 5000):
    loop = QEventLoop()
    timed_out = [False]
    poll = QTimer()
    poll.setInterval(20)
    watchdog = QTimer()
    watchdog.setSingleShot(True)

    def check():
        if predicate():
            poll.stop()
            watchdog.stop()
            loop.quit()

    def abort():
        timed_out[0] = True
        poll.stop()
        loop.quit()

    poll.timeout.connect(check)
    watchdog.timeout.connect(abort)
    poll.start()
    watchdog.start(timeout_ms)
    loop.exec()
    assert not timed_out[0], 'Timed out waiting for Qt async condition'


def test_guide_specs_and_workspace_region_sync(tmp_path):
    adapter, fake, editor, a1, a2 = _make_adapter_rig(tmp_path)
    guides = build_region_guides(editor.w)
    assert len(guides) == 2
    by_id = {g['id']: g for g in guides}
    assert by_id[a1]['label'] == f'[CoLayout AI Region: Core_A ({a1[:12]})] DO NOT EDIT INSIDE'
    assert ascii_guide_label(editor.w.local_data.regions[a2]) == f'[CoLayout AI Region: {a2[:12]}] DO NOT EDIT INSIDE'

    ws_files = WorkspaceFiles(
        lambda: editor,
        lambda: tmp_path / 'ws.colayout.json',
        lambda *_: None,
        tmp_path / 'archive',
        native=lambda: adapter,
    )
    session = AgentSession(
        lambda: editor,
        lambda: None,
        tmp_path / 'agent-runs',
        workspace_files=ws_files,
    )
    status = session.invoke('workspace', {'action': 'status'}, client_id='ai-1')['result']
    assert len(status['regions']) == 2

    created = session.invoke(
        'workspace',
        {
            'action': 'create_region',
            'expected_document_id': status['document_id'],
            'expected_revision': status['revision'],
            'bounds_um': [33.0, 0.0, 37.0, 3.0],
            'name': 'Aux_Driver',
            'pin_mode': 'directional',
        },
        client_id='ai-1',
    )
    assert created['ok'] is True
    rid3 = created['result']['region_id']
    assert editor.w.local_data.regions[rid3].pin_mode == 'directional'
    assert created['result']['guides_sync']['synced'] is True
    assert len(fake.guides) == 3
    assert any(g['id'] == rid3 and g['bbox'] == [['33000', '0'], ['37000', '3000']] for g in fake.guides)

    # Dissolve region and confirm Virtuoso guides shrink back cleanly
    dissolved = session.invoke(
        'workspace',
        {
            'action': 'dissolve_region',
            'expected_document_id': created['result']['document_id'],
            'expected_revision': created['result']['revision'],
            'region_id': rid3,
        },
        client_id='ai-1',
    )
    assert dissolved['ok'] is True
    assert len(fake.guides) == 2
    assert all(g['id'] != rid3 for g in fake.guides)

    # Manual sync_regions also pushes current frames explicitly
    manual = session.invoke('workspace', {'action': 'sync_regions'}, client_id='ai-1')
    assert manual['ok'] is True and manual['result']['guides_synced'] is True
    assert len(fake.guides) == 2


def test_cooperative_pull_merges_outside_edits_and_keeps_active_lease(tmp_path):
    adapter, fake, editor, a1, _a2 = _make_adapter_rig(tmp_path)
    ws_files = WorkspaceFiles(
        lambda: editor,
        lambda: tmp_path / 'ws.colayout.json',
        lambda *_: None,
        tmp_path / 'archive',
        native=lambda: adapter,
    )
    session = AgentSession(
        lambda: editor,
        lambda: None,
        tmp_path / 'agent-runs',
        workspace_files=ws_files,
    )
    ws_files.on_cooperative_baseline = session.ownership.on_cooperative_baseline

    # AI claims area-1 and draws an uncommitted wire inside area-1
    claim = session.invoke('claim_region', {'region_id': a1, 'agent_name': 'router-1'}, client_id='ai-1')
    assert claim['ok'] is True
    tok = claim['result']['session_token']
    edit1 = session.invoke(
        'edit_region',
        {
            'session_token': tok,
            'expected_revision': claim['result']['revision'],
            'operations': [
                {
                    'action': 'create_rect',
                    'params': {'x': 3.5, 'y': 1.0, 'width': 1.0, 'height': 1.0, 'lpp': ['M1', 'drawing'], 'net_intent': 'NET_M_A'},
                }
            ],
        },
        client_id='ai-1',
    )
    assert edit1['ok'] is True
    w_ai_1 = edit1['result']['created_uids'][0]

    # Meanwhile Human in Virtuoso adds an outside wire without colayoutObjectId, modifies ext_edit, and deletes ext_del
    v_objs = dict(fake.current.objects)
    v_objs['session:human_new'] = _rect_obj(
        'session:human_new', 22000, 3000, 24000, 4000, lpp=('M2', 'drawing'), net='EXT_H', name='H_NEW', missing_uid=True
    )
    v_objs['ext_edit'] = _rect_obj('ext_edit', 25000, 2000, 27500, 3000, lpp=('M1', 'drawing'), net='EXT2', name='EXT_EDIT')
    v_objs.pop('ext_del')
    fake.current = fake.current.model_copy(update={'objects': v_objs, 'counter': '2'})

    st = session.invoke('workspace', {'action': 'status'}, client_id='ai-1')['result']
    pulled = session.invoke(
        'workspace',
        {
            'action': 'pull',
            'expected_document_id': st['document_id'],
            'expected_revision': st['revision'],
        },
        client_id='ai-1',
    )
    assert pulled['ok'] is True
    assert pulled['result']['pulled'] is True
    assert pulled['result']['last_result']['virtuoso_delta'] == {'added': 1, 'modified': 1, 'deleted': 1}
    # Verify Virtuoso edits landed AND AI's uncommitted wire inside area-1 survived
    assert 'prep:1' in editor.w.baseline.objects
    assert 'ext_del' not in editor.w.baseline.objects
    assert editor.w.baseline.objects['ext_edit'].shape.bbox == ((25000, 2000), (27500, 3000))
    assert any(op.uid == w_ai_1 for op in editor.w.operations)
    assert w_ai_1 in projected(editor.w)
    # Active lease stayed alive and AI can immediately continue editing at the new revision
    assert editor.work_sessions.session_for(a1) is not None
    edit2 = session.invoke(
        'edit_region',
        {
            'session_token': tok,
            'expected_revision': pulled['result']['revision'],
            'operations': [
                {
                    'action': 'create_rect',
                    'params': {'x': 0.5, 'y': 3.5, 'width': 1.0, 'height': 1.0, 'lpp': ['M2', 'drawing'], 'net_intent': 'NET_M_A'},
                }
            ],
        },
        client_id='ai-1',
    )
    assert edit2['ok'] is True
    assert len(editor.w.operations) == 2


def test_cooperative_commit_merges_outside_virtuoso_and_inside_colayout(tmp_path):
    adapter, fake, editor, a1, _a2 = _make_adapter_rig(tmp_path)
    editor.scope_id = a1
    w_ai = editor.create(
        Shape(kind='rect', lpp=('M1', 'drawing'), bbox=((3500, 1000), (4500, 2000)), net_intent='NET_M_A'),
        uid='ai-wire-1',
    )
    assert w_ai == 'ai-wire-1'
    draft = editor.w

    # Human edits Virtuoso outside regions (adds unprepared wire, deletes ext_del)
    v_objs = dict(fake.current.objects)
    v_objs['session:h1'] = _rect_obj(
        'session:h1', 22000, 3000, 24000, 4000, lpp=('M2', 'drawing'), net='EXT_H', name='H1', missing_uid=True
    )
    v_objs.pop('ext_del')
    fake.current = fake.current.model_copy(update={'objects': v_objs, 'counter': '2'})

    req_id = 'coop-commit-1'
    committed = adapter.commit(draft, request_id=req_id)
    assert committed.state == 'clean'
    assert committed.operations == ()
    # Both Human's outside wire and AI's inside wire exist on Virtuoso and in merged baseline
    assert 'ai-wire-1' in fake.current.objects
    assert 'prep:1' in fake.current.objects
    assert 'ext_del' not in fake.current.objects
    assert committed.baseline.objects['ai-wire-1'].shape == draft.operations[0].shape

    # Idempotent replay with same draft & request_id never executes twice
    replayed = adapter.commit(draft, request_id=req_id)
    assert replayed.state == 'clean'
    assert fake.submissions == 1

    # Reusing request_id with altered draft still fails strictly
    editor.scope_id = a1
    editor.create(
        Shape(kind='rect', lpp=('M1', 'drawing'), bbox=((3500, 2500), (4500, 3500)), net_intent='NET_M_A'),
        uid='ai-wire-2',
    )
    with pytest.raises(LiteError, match='REQUEST_ID_REUSED'):
        adapter.commit(editor.w, request_id=req_id)

    # Confirm rollback on cooperative rebase preserves Virtuoso outside merge + AI local draft
    adapter_rb, fake_rb, editor_rb, a1_rb, _ = _make_adapter_rig(tmp_path / 'rb')
    editor_rb.scope_id = a1_rb
    editor_rb.create(
        Shape(kind='rect', lpp=('M1', 'drawing'), bbox=((3500, 1000), (4500, 2000)), net_intent='NET_M_A'),
        uid='rb-wire',
    )
    v_objs_rb = dict(fake_rb.current.objects)
    v_objs_rb['ext_edit'] = _rect_obj('ext_edit', 25000, 2000, 27000, 3000, net='EXT2', name='EXT_EDIT')
    fake_rb.current = fake_rb.current.model_copy(update={'objects': v_objs_rb, 'counter': '2'})
    fake_rb.behavior = 'rolled_back'
    rb_res = adapter_rb.commit(editor_rb.w, request_id='rb-1')
    assert rb_res.state == 'editing'
    assert len(rb_res.operations) == 1
    assert rb_res.baseline.objects['ext_edit'].shape.bbox == ((25000, 2000), (27000, 3000))
    fake_rb.behavior = 'saved'
    assert adapter_rb.commit(rb_res, request_id='rb-2').state == 'clean'


def test_scoped_region_sync_ui_and_release_sync_to_virtuoso(qapp, tmp_path, monkeypatch):
    adapter, fake, editor, a1, a2 = _make_adapter_rig(tmp_path)
    # AI claims BOTH a1 and a2 and makes local edits in each
    ws_files = WorkspaceFiles(
        lambda: editor,
        lambda: tmp_path / 'ws.colayout.json',
        lambda *_: None,
        tmp_path / 'archive',
        native=lambda: adapter,
    )
    session = AgentSession(
        lambda: editor,
        lambda: None,
        tmp_path / 'agent-runs',
        workspace_files=ws_files,
    )
    ws_files.on_cooperative_baseline = session.ownership.on_cooperative_baseline

    c1 = session.invoke('claim_region', {'region_id': a1, 'agent_name': 'ai-a1'}, client_id='c1')['result']
    c2 = session.invoke('claim_region', {'region_id': a2, 'agent_name': 'ai-a2'}, client_id='c2')['result']
    e1 = session.invoke(
        'edit_region',
        {
            'session_token': c1['session_token'],
            'expected_revision': c1['revision'],
            'operations': [{'action': 'create_rect', 'params': {'x': 3.5, 'y': 1.0, 'width': 1.0, 'height': 1.0, 'lpp': ['M1', 'drawing'], 'net_intent': 'NET_M_A'}}],
        },
        client_id='c1',
    )['result']
    e2 = session.invoke(
        'edit_region',
        {
            'session_token': c2['session_token'],
            'expected_revision': c2['revision'],
            'operations': [{'action': 'create_rect', 'params': {'x': 15.5, 'y': 1.0, 'width': 1.0, 'height': 1.0, 'lpp': ['M1', 'drawing'], 'net_intent': 'NET_M_B'}}],
        },
        client_id='c2',
    )['result']
    u1 = e1['created_uids'][0]
    u2 = e2['created_uids'][0]

    # Release a1, keep a2 actively leased!
    rel1 = session.invoke('release_region', {'session_token': c1['session_token'], 'outcome': 'handoff'}, client_id='c1')
    assert rel1['ok'] is True

    # Invalid combo: sync_to_virtuoso=True requires outcome='done'
    bad_rel = session.invoke(
        'release_region',
        {'session_token': c2['session_token'], 'outcome': 'cancelled', 'sync_to_virtuoso': True},
        client_id='c2',
    )
    assert bad_rel['ok'] is False and bad_rel['error']['code'] == 'INVALID_ARGUMENTS'

    # While a2 is STILL leased, sync ONLY a1 -> Virtuoso receives u1, while a2 keeps u2 locally & stays leased!
    st = session.invoke('workspace', {'action': 'status'}, client_id='c1')['result']
    synced1 = session.invoke(
        'workspace',
        {
            'action': 'sync',
            'expected_document_id': st['document_id'],
            'expected_revision': st['revision'],
            'region_id': a1,
        },
        client_id='c1',
    )
    assert synced1['ok'] is True
    assert u1 in fake.current.objects
    assert u2 not in fake.current.objects
    assert len(editor.w.operations) == 1 and editor.w.operations[0].uid == u2
    assert editor.work_sessions.session_for(a2) is not None

    # Test release_region(outcome='done', sync_to_virtuoso=True) with Virtuoso intrusion first:
    # release succeeds (so client session clears cleanly) and virtuoso_sync reports VIRTUOSO_EDIT_IN_REGION!
    monkeypatch.setattr(
        'colayout_lite.agent_tools.release_status.verification',
        lambda _sess, _lease: {'state': 'passed', 'next_action': 'ok'},
    )
    bad_v = dict(fake.current.objects)
    bad_v['session:intrude'] = _rect_obj('session:intrude', 15000, 3500, 16500, 4500, lpp=('M2', 'drawing'), name='BAD_M2', missing_uid=True)
    fake.current = fake.current.model_copy(update={'objects': bad_v, 'counter': '20'})
    rel_intruded = session.invoke(
        'release_region',
        {'session_token': c2['session_token'], 'outcome': 'done', 'sync_to_virtuoso': True},
        client_id='c2',
    )
    assert rel_intruded['ok'] is True and rel_intruded['result']['released'] is True
    assert rel_intruded['result']['virtuoso_sync']['synced'] is False
    assert rel_intruded['result']['virtuoso_sync']['error']['code'] == 'VIRTUOSO_EDIT_IN_REGION'
    assert u2 not in fake.current.objects

    # Fix Virtuoso intrusion, then verify real Qt StudioWindow UI sync/pull + active lease behavior
    clean_v = dict(fake.current.objects)
    clean_v.pop('session:intrude')
    clean_v['ext_ok'] = _rect_obj('ext_ok', 22000, 3500, 24000, 4500, net='EXT_OK', name='EXT_OK')
    fake.current = fake.current.model_copy(update={'objects': clean_v, 'counter': '21'})

    doc_path = tmp_path / 'studio.colayout.json'
    save_document(doc_path, editor.w, editor.profile)
    win = StudioWindow(
        doc_path,
        shortcut_path=tmp_path / 'gui-shortcuts.json',
        storage=WorkspaceStorage(tmp_path / 'runtime'),
    )
    try:
        win.adapter = adapter
        win.show()
        QApplication.processEvents()
        hub = enable_agent_hub(win, folder=tmp_path / 'hub-runs', connection_file=tmp_path / 'hub-conn.json')
        # Sync button remains enabled even when a1 has an active lease while u2 is in a2!
        c1_ui = hub.runtime.invoke('claim_region', {'region_id': a1, 'agent_name': 'ui-watcher'}, client_id='ui-c1')['result']
        assert win.pull_button.isEnabled() and win.sync_button.isEnabled()

        # Trigger UI sync(): pushes a2's u2 AND pulls Human's outside ext_ok while keeping a1's lease alive!
        win.sync()
        _wait_qt_until(lambda: not win.busy and u2 in fake.current.objects)
        assert u2 in win.editor.w.baseline.objects
        assert 'ext_ok' in win.editor.w.baseline.objects
        assert not win.editor.w.operations
        assert win.editor.work_sessions.session_for(a1) is not None
        # Lease on a1 can still execute edits seamlessly!
        ui_rev = win.editor.w.revision
        ed_after = hub.runtime.invoke(
            'edit_region',
            {
                'session_token': c1_ui['session_token'],
                'expected_revision': ui_rev,
                'operations': [{'action': 'create_rect', 'params': {'x': 3.5, 'y': 3.5, 'width': 1.0, 'height': 1.0, 'lpp': ['M2', 'drawing'], 'net_intent': 'NET_M_A'}}],
            },
            client_id='ui-c1',
        )
        assert ed_after['ok'] is True
    finally:
        win.close()
        QApplication.processEvents()


def test_virtuoso_edit_inside_region_blocks_both_pull_and_commit(tmp_path):
    adapter, fake, editor, a1, _a2 = _make_adapter_rig(tmp_path)
    editor.scope_id = a1
    editor.create(
        Shape(kind='rect', lpp=('M1', 'drawing'), bbox=((3500, 1000), (4500, 2000)), net_intent='NET_M_A'),
        uid='ai-inside',
    )
    draft = editor.w

    # Case A: Human creates an unprepared M2 rect inside area-1 (0..5 um, 0..5 um)
    bad_objs = dict(fake.current.objects)
    bad_objs['session:bad_add'] = _rect_obj(
        'session:bad_add', 500, 500, 1500, 1500, lpp=('M2', 'drawing'), name='HUMAN_BAD_RECT', missing_uid=True
    )
    fake.current = fake.current.model_copy(update={'objects': bad_objs, 'counter': '2'})

    for call in (lambda: adapter.cooperative_pull(draft), lambda: adapter.commit(draft, request_id=uuid4().hex)):
        with pytest.raises(LiteError) as err:
            call()
        assert err.value.code == 'VIRTUOSO_EDIT_IN_REGION'
        assert 'HUMAN_BAD_RECT' in str(err.value)
        assert 'Core_A' in str(err.value)
        assert err.value.details['intrusions'][0]['lpp'] == ['M2', 'drawing']
    assert fake.submissions == 0
    assert not (adapter.state / 'transactions').exists()
    assert not any(name == 'ltPrepare' for name, _ in fake.calls)

    # Case B: Human moves previously-outside ext_edit INTO area-1 (overlap both via old outside + new inside!)
    moved_objs = {k: v for k, v in fake.current.objects.items() if k != 'session:bad_add'}
    moved_objs['ext_edit'] = _rect_obj('ext_edit', 4000, 3500, 6000, 4500, lpp=('M1', 'drawing'), net='EXT2', name='MOVED_INTO_REGION')
    fake.current = fake.current.model_copy(update={'objects': moved_objs, 'counter': '3'})
    with pytest.raises(LiteError, match='VIRTUOSO_EDIT_IN_REGION') as err_move:
        adapter.commit(draft, request_id='move-intrude')
    assert err_move.value.details['intrusions'][0]['change'] == 'modified'
    assert err_move.value.details['intrusions'][0]['region_id'] == a1

    # Case C: Human deletes M_A (inside area-1) in Virtuoso
    del_objs = dict(fake.current.objects)
    del_objs['ext_edit'] = _initial_snapshot_and_context(_coop_profile())[0].objects['ext_edit']
    del_objs.pop('inst-M_A')
    fake.current = fake.current.model_copy(update={'objects': del_objs, 'counter': '4'})
    with pytest.raises(LiteError, match='VIRTUOSO_EDIT_IN_REGION') as err_del:
        adapter.cooperative_pull(draft)
    assert err_del.value.details['intrusions'][0]['change'] == 'deleted'
    assert 'M_A' in str(err_del.value)

    # Restore clean inside region while keeping Human's valid outside wire -> retry succeeds!
    restored = dict(_initial_snapshot_and_context(_coop_profile())[0].objects)
    restored['session:clean_outside'] = _rect_obj(
        'session:clean_outside', 22000, 3000, 24000, 4000, lpp=('M2', 'drawing'), name='CLEAN_OUT', missing_uid=True
    )
    fake.current = fake.current.model_copy(update={'objects': restored, 'counter': '5'})
    pulled, _ = adapter.cooperative_pull(draft)
    assert 'prep:1' in pulled.baseline.objects
    assert len(pulled.operations) == 1
    committed = adapter.commit(pulled, request_id='recovered-coop')
    assert committed.state == 'clean'
    assert 'ai-inside' in fake.current.objects and 'prep:1' in fake.current.objects


def test_unsaved_virtuoso_changes_rejected(tmp_path):
    adapter, fake, editor, a1, _a2 = _make_adapter_rig(tmp_path)
    editor.scope_id = a1
    editor.create(
        Shape(kind='rect', lpp=('M1', 'drawing'), bbox=((3500, 1000), (4500, 2000)), net_intent='NET_M_A'),
        uid='u-unsaved',
    )
    fake.current = fake.current.model_copy(update={'dirty': True})

    with pytest.raises(LiteError) as err1:
        adapter.cooperative_pull(editor.w)
    assert err1.value.code == 'VIRTUOSO_UNSAVED_CHANGES'

    with pytest.raises(LiteError) as err2:
        adapter.commit(editor.w, request_id='unsaved-tx')
    assert err2.value.code == 'VIRTUOSO_UNSAVED_CHANGES'
    assert fake.submissions == 0
    assert not any(name in ('ltPrepare', 'ltTransaction') for name, _ in fake.calls)


def test_partial_cooperative_commit_reconcile_and_ui_auto_guide_sync(qapp, tmp_path):
    adapter, fake, editor, a1, a2 = _make_adapter_rig(tmp_path)
    editor.scope_id = a1
    u1 = editor.create(
        Shape(kind='rect', lpp=('M1', 'drawing'), bbox=((3500, 1000), (4500, 2000)), net_intent='NET_M_A'),
        uid='u-part-1',
    )
    editor.scope_id = a2
    u2 = editor.create(
        Shape(kind='rect', lpp=('M1', 'drawing'), bbox=((15500, 1000), (16500, 2000)), net_intent='NET_M_B'),
        uid='u-part-2',
    )
    editor.scope_id = None

    # Human changes outside region concurrently, while tx reply gets lost after apply!
    v_objs = dict(fake.current.objects)
    v_objs['ext_keep'] = _rect_obj('ext_keep', 22000, 1000, 24000, 2000, net='EXT1', name='EXT_KEEP')
    fake.current = fake.current.model_copy(update={'objects': v_objs, 'counter': '2'})
    fake.behavior = 'lost_after_apply'

    with pytest.raises(TimeoutError):
        adapter.commit(editor.w, request_id='partial-lost', scope_region_ids={a1})
    pending = adapter.load_workspace(editor.w.id)
    assert pending.state == 'indeterminate'

    # Reconcile validates tx_w (only u1 + Virtuoso ext_keep) and preserves retained u2 in a2!
    recovered = adapter.reconcile(pending)
    assert recovered.state == 'editing'
    assert recovered.last_result['status'] == 'saved_verified'
    assert recovered.last_result['reconciled'] is True
    assert u1 in recovered.baseline.objects
    assert recovered.baseline.objects['ext_keep'].shape.bbox == ((22000, 1000), (24000, 2000))
    assert [op.uid for op in recovered.operations] == [u2]

    # Verify Qt StudioWindow automatically pushes guide updates when a region is created/dissolved via UI
    doc_path = tmp_path / 'ui-guide.colayout.json'
    save_document(doc_path, recovered, editor.profile)
    win = StudioWindow(
        doc_path,
        shortcut_path=tmp_path / 'gui-shortcuts.json',
        storage=WorkspaceStorage(tmp_path / 'runtime'),
    )
    try:
        win.adapter = adapter
        win.show()
        QApplication.processEvents()
        prev_len = len(fake.guides)
        assert prev_len == 0
        rid3 = RegionActions(win.editor).create((33000, 0, 37000, 3000), 'UI_Aux')
        _wait_qt_until(lambda: len(fake.guides) == 3)
        assert any(g['id'] == rid3 for g in fake.guides)
        RegionActions(win.editor).dissolve(rid3)
        _wait_qt_until(lambda: len(fake.guides) == 2)
    finally:
        win.close()
        QApplication.processEvents()


def test_cooperative_pull_and_sync_with_outside_native_route_changes_and_split(tmp_path):
    adapter, fake, editor, a1, _a2 = _make_adapter_rig(tmp_path)
    editor.scope_id = a1
    ai_uid = editor.create(
        Shape(kind='rect', lpp=('M1', 'drawing'), bbox=((3500, 1000), (4500, 2000)), net_intent='NET_M_A'),
        uid='ai-r-keep',
    )
    editor.scope_id = None

    # 1) Outside-region NativeRoute added + outside pathSeg modified/added in Virtuoso
    v_objs = dict(fake.current.objects)
    ext_seg = Object(
        uid='session:ext_seg',
        native_id='nat:ext_seg',
        kind='pathSeg',
        bbox=((22000, 500), (24000, 500)),
        shape=Shape(kind='pathSeg', lpp=('M1', 'drawing'), points=((22000, 500), (24000, 500)), width=200, net_intent='EXT1'),
        attributes={'name': 'EXT_ROUTE_SEG'},
        properties=[['colayoutNetIntent', 'string', ['string', 'EXT1']]],
        observed_net='EXT1',
        route_uid='r-ext',
        read_only_reason='missing_persistent_identity',
    )
    v_objs['session:ext_seg'] = ext_seg
    v_routes = {
        'r-ext': NativeRoute(uid='r-ext', members=('session:ext_seg',), status='normal', net='EXT1')
    }
    fake.current = fake.current.model_copy(
        update={
            'objects': v_objs,
            'routes': v_routes,
            'opaque_counts': {**fake.current.opaque_counts, 'routes': 1},
            'counter': '2',
        }
    )

    # Simulate split route duplicate ID on initial read before ltPrepare runs
    orig_read = fake.read
    split_injected = {'pending': True}

    def read_with_split(lib, cell):
        if split_injected['pending']:
            raise LiteError('DUPLICATE_ROUTE_ID', 'r-ext')
        return orig_read(lib, cell)

    orig_call = fake.call

    def call_with_split(name, *values):
        res = orig_call(name, *values)
        if name == 'ltPrepare' and split_injected['pending']:
            split_injected['pending'] = False
            fixed_routes = {
                'prep:route:1': NativeRoute(uid='prep:route:1', members=('prep:1',), status='normal', net='EXT1')
            }
            objs = {
                k: (v.model_copy(update={'route_uid': 'prep:route:1'}) if k == 'prep:1' else v)
                for k, v in fake.current.objects.items()
            }
            fake.current = fake.current.model_copy(update={'objects': objs, 'routes': fixed_routes})
        return res

    adapter.read = read_with_split
    fake.read = read_with_split
    fake.call = call_with_split

    pulled, _ = adapter.cooperative_pull(editor.w)
    assert any(name == 'ltPrepare' for name, _ in fake.calls)
    assert 'prep:1' in pulled.baseline.objects
    assert 'prep:route:1' in (pulled.baseline.routes or {})
    assert [op.uid for op in pulled.operations] == [ai_uid]

    # 2) Route-level change touching an inside-region object raises VIRTUOSO_EDIT_IN_REGION cleanly (no AttributeError)
    in_seg = Object(
        uid='in-seg',
        native_id='nat:in_seg',
        kind='pathSeg',
        bbox=((1000, 1500), (2000, 1500)),
        shape=Shape(kind='pathSeg', lpp=('M1', 'drawing'), points=((1000, 1500), (2000, 1500)), width=200, net_intent='NET_M_A'),
        attributes={'name': 'IN_ROUTE_SEG'},
        properties=[['colayoutObjectId', 'string', ['string', 'in-seg']], ['colayoutNetIntent', 'string', ['string', 'NET_M_A']]],
        observed_net='NET_M_A',
        route_uid='r-in',
    )
    bad_base = pulled.baseline.model_copy(
        update={
            'objects': {**pulled.baseline.objects, 'in-seg': in_seg},
            'routes': {**(pulled.baseline.routes or {}), 'r-in': NativeRoute(uid='r-in', members=('in-seg',), status='normal', net='NET_M_A')},
            'opaque_counts': {**pulled.baseline.opaque_counts, 'routes': 2},
        }
    )
    bad_regs = dict(pulled.local_data.regions)
    bad_regs[a1] = bad_regs[a1].model_copy(update={'object_uids': (*bad_regs[a1].object_uids, 'in-seg')})
    bad_w = pulled.model_copy(
        update={'baseline': bad_base, 'local_data': pulled.local_data.model_copy(update={'regions': bad_regs})}
    )
    fake.current = bad_base.model_copy(
        update={
            'routes': {**(bad_base.routes or {}), 'r-in': NativeRoute(uid='r-in', members=('in-seg',), status='locked', net='NET_M_A')},
            'counter': '4',
        }
    )
    with pytest.raises(LiteError) as err_route:
        adapter.cooperative_pull(bad_w)
    assert err_route.value.code == 'VIRTUOSO_EDIT_IN_REGION'
    assert err_route.value.details['intrusions'][0]['change'] == 'route_member'
    assert err_route.value.details['intrusions'][0]['region_id'] == a1


def test_sync_regions_leaves_virtuoso_clean_and_counter_unchanged(tmp_path):
    prof = _coop_profile()
    snap, circuit, connectivity = _initial_snapshot_and_context(prof)
    fake = FakeCoopNative(tmp_path, snap, prof, circuit, connectivity)
    adapter = Adapter(fake)
    adapter.read = fake.read
    editor = Editor(
        Workspace(baseline=snap, profile_id=prof.id, circuit=circuit, layout_connectivity=connectivity, connection_config=str(fake.config_path)),
        prof,
    )
    doc = tmp_path / 'doc.colayout.json'
    save_document(doc, editor.w, prof)
    archive = tmp_path / 'workspaces'
    archive.mkdir()
    files = WorkspaceFiles(
        lambda: editor,
        lambda: doc,
        lambda p: None,
        archive,
        storage=WorkspaceStorage(tmp_path / 'storage'),
        native=lambda: adapter,
    )
    before_counter = fake.current.counter
    created = files.invoke(files_args := __import__('colayout_lite.agent_tools.contract', fromlist=['WorkspaceFile']).WorkspaceFile(
        action='create_region',
        expected_document_id=editor.w.id,
        expected_revision=editor.w.revision,
        bounds_um=(33.0, 0.0, 37.0, 3.0),
        name='CleanGuide',
    ))
    assert created['guides_sync']['synced'] is True
    assert fake.current.counter == before_counter
    assert fake.current.dirty is False

    synced = files.invoke(__import__('colayout_lite.agent_tools.contract', fromlist=['WorkspaceFile']).WorkspaceFile(action='sync_regions'))
    assert synced['guides_synced'] is True
    assert fake.current.counter == before_counter
    assert fake.current.dirty is False

    pulled = files.invoke(type(files_args)(
        action='pull', expected_document_id=editor.w.id, expected_revision=editor.w.revision
    ))
    assert pulled['pulled'] is True


def test_consecutive_create_guard_without_sync_and_with_outside_virtuoso_edits(tmp_path):
    prof = _coop_profile().model_copy(
        update={
            'guard_templates': [
                {'library': 'TECH', 'name': 'pguard', 'definition': '(("M1" "drawing") nil nil nil)'}
            ]
        }
    )
    snap, circuit, connectivity = _initial_snapshot_and_context(prof)

    class GuardCoopNative(FakeCoopNative):
        def __init__(self):
            super().__init__(tmp_path, snap, prof, circuit, connectivity)
            self.cells = {(snap.library, snap.cell): snap}
            self.guide_cells_called = []

        def read(self, library, cell):
            return self.cells[(library, cell)], self.profile

        def call(self, name, *values):
            if name == 'lcClonePaired':
                lib, src_cell, dst_cell, _seed = values
                src = self.cells[(lib, src_cell)]
                self.cells[(lib, dst_cell)] = src.model_copy(update={'cell': dst_cell, 'counter': '10'})
                self.calls.append((name, values))
                return ['copied', dst_cell]
            if name == 'ltSyncRegionGuides':
                lib, cell, guides = values
                self.guide_cells_called.append(cell)
                return super().call(name, lib, cell, guides)
            if name == 'ltRelease':
                return [1, 1, 0]
            return super().call(name, *values)

        def transact(self, library, cell, request):
            cv = self.cells[(library, cell)]
            saved_current = self.current
            self.current = cv
            try:
                old_objs = dict(self.current.objects)
                # Temporarily rewrite guard op to handle guard natively after standard checks
                normal_req = dict(request)
                guard_ops = [cmd for cmd in request['operations'] if cmd['action'] == 'guard']
                normal_req['operations'] = [cmd for cmd in request['operations'] if cmd['action'] != 'guard']
                receipt = super().transact(library, cell, normal_req)
                if guard_ops:
                    objs = dict(self.current.objects)
                    for cmd in guard_ops:
                        guid = cmd['uid']
                        spec = cmd['guard']
                        pts = [[int(p[0]), int(p[1])] for p in spec['points']]
                        xs = [p[0] for p in pts]
                        ys = [p[1] for p in pts]
                        bbox = ((min(xs), min(ys)), (max(xs), max(ys)))
                        master = Shape(kind='path', lpp=('M1', 'drawing'), points=tuple(tuple(p) for p in pts), width=200, net_intent=spec.get('net_intent'))
                        objs[guid] = Object(
                            uid=guid,
                            native_id=f'nat:{guid}',
                            kind='path',
                            bbox=bbox,
                            mpp=MultipartPath(
                                master=master,
                                name=spec['template'],
                                parts=[{'lpp': ['M1', 'drawing'], 'kind': 'rect', 'net_intent': spec.get('net_intent')}],
                                native_definition=spec['definition'],
                            ),
                            attributes={'name': guid[:8]},
                            properties=[
                                ['colayoutObjectId', 'string', ['string', guid]],
                                ['colayoutStructureUid', 'string', ['string', guid]],
                                ['colayoutNetIntent', 'string', ['string', spec.get('net_intent') or '']],
                            ],
                            observed_net=spec.get('net_intent'),
                            group_uid=guid,
                            read_only_reason='protected_group',
                        )
                    next_c = str(int(self.current.counter) + 1)
                    self.current = self.current.model_copy(update={'objects': objs, 'counter': next_c})
                self.cells[(library, cell)] = self.current
                if (library, cell) == (saved_current.library, saved_current.cell):
                    saved_current = self.current
                return receipt
            finally:
                self.current = saved_current

    fake = GuardCoopNative()
    adapter = Adapter(fake)
    adapter.read = fake.read

    def fake_read_pair(lib, cell, *, schematic=None):
        s, p = fake.read(lib, cell)
        c = fake.circuit.model_copy(update={'source': fake.circuit.source.model_copy(update={'library': lib, 'cell': cell, 'counter': s.counter})})
        conn = fake.connectivity.model_copy(update={'source': fake.connectivity.source.model_copy(update={'library': lib, 'cell': cell, 'counter': s.counter})})
        return s, p, c, conn

    adapter.read_pair = fake_read_pair
    fake.read_pair = fake_read_pair

    editor = Editor(
        Workspace(baseline=snap, profile_id=prof.id, circuit=circuit, layout_connectivity=connectivity, connection_config=str(fake.config_path)),
        prof,
    )
    a1 = RegionActions(editor).create((-5000, -5000, 10000, 10000), 'Core_G')
    # Expand a2 separately isn't needed; make editor active under a1 work_session too!
    session = editor.work_sessions.start(a1, 'agent-guard', expected_revision=editor.w.revision)
    editor.scope_id = a1
    tpl = prof.guard_templates[0]

    # Guard #1 inside region
    with editor.work_sessions.actor(session.id, expected_revision=editor.w.revision, expected_pose_revision=session.pose_revision):
        u1 = adapter.guard_preview(editor, bbox=((500, 500), (2500, 2500)), template=tpl, net_intent='VDD')
    # Consecutive Guard #2 WITHOUT syncing to Virtuoso -> no temporary guide sync, no SOURCE_CONFLICT!
    with editor.work_sessions.actor(session.id, expected_revision=editor.w.revision, expected_pose_revision=session.pose_revision):
        u2 = adapter.guard_preview(editor, bbox=((4000, 500), (6000, 2500)), template=tpl, net_intent='VSS')

    assert [op.uid for op in editor.w.operations] == [u1, u2]
    assert set(editor.w.guard_previews) == {u1, u2}
    assert all(not c.startswith('COLAYOUT_MVP_LITE_') for c in fake.guide_cells_called)

    # Human concurrently edits an outside-region wire + route in Virtuoso WITHOUT CoLayout pulling first -> Guard #3 still works!
    main_cv = fake.cells[(snap.library, snap.cell)]
    ext_seg = Object(
        uid='ext-guard-co-seg',
        native_id='nat:ext_gc',
        kind='pathSeg',
        bbox=((22000, 2000), (24000, 2000)),
        shape=Shape(kind='pathSeg', lpp=('M1', 'drawing'), points=((22000, 2000), (24000, 2000)), width=200, net_intent='EXT1'),
        properties=[['colayoutObjectId', 'string', ['string', 'ext-guard-co-seg']], ['colayoutNetIntent', 'string', ['string', 'EXT1']]],
        observed_net='EXT1',
        route_uid='r-ext-g',
    )
    fake.cells[(snap.library, snap.cell)] = main_cv.model_copy(
        update={
            'objects': {**main_cv.objects, 'ext-guard-co-seg': ext_seg},
            'routes': {'r-ext-g': NativeRoute(uid='r-ext-g', members=('ext-guard-co-seg',), status='normal', net='EXT1')},
            'opaque_counts': {**main_cv.opaque_counts, 'routes': 1},
            'counter': '2',
        }
    )
    fake.current = fake.cells[(snap.library, snap.cell)]
    with editor.work_sessions.actor(session.id, expected_revision=editor.w.revision, expected_pose_revision=session.pose_revision):
        u3 = adapter.guard_preview(editor, bbox=((4000, 4000), (6000, 6000)), template=tpl, net_intent='VDD')
    assert [op.uid for op in editor.w.operations] == [u1, u2, u3]
    assert set(editor.w.guard_previews) == {u1, u2, u3}

    # Intruding edit inside region still blocks guard_preview with VIRTUOSO_EDIT_IN_REGION
    bad_cv = fake.cells[(snap.library, snap.cell)]
    fake.cells[(snap.library, snap.cell)] = bad_cv.model_copy(
        update={
            'objects': {**bad_cv.objects, 'session:bad-g': _rect_obj('session:bad-g', -1000, -1000, 0, 0, missing_uid=True)},
            'counter': '3',
        }
    )
    fake.current = fake.cells[(snap.library, snap.cell)]
    with pytest.raises(LiteError) as err_g:
        with editor.work_sessions.actor(session.id, expected_revision=editor.w.revision, expected_pose_revision=session.pose_revision):
            adapter.guard_preview(editor, bbox=((500, 4000), (2500, 6000)), template=tpl, net_intent='VDD')
    assert err_g.value.code == 'VIRTUOSO_EDIT_IN_REGION'


def test_snapshot_overwrite_ignores_edit_history_and_preserves_untouched_region_objects(tmp_path):
    from colayout_lite.mpp import placed

    prof = _coop_profile().model_copy(
        update={
            'guard_templates': [
                {'library': 'TECH', 'name': 'pguard', 'definition': '(("M1" "drawing") nil nil nil)'}
            ]
        }
    )
    snap, circuit, connectivity = _initial_snapshot_and_context(prof)

    in_route_keep = Object(
        uid='in-route-untouched',
        native_id='nat:in_route_keep',
        kind='pathSeg',
        bbox=((200, 200), (800, 200)),
        shape=Shape(
            kind='pathSeg',
            lpp=('M1', 'drawing'),
            points=((200, 200), (800, 200)),
            width=100,
            net_intent='NET_M_A',
        ),
        attributes={'name': 'IN_ROUTE_KEEP'},
        properties=[
            ['colayoutObjectId', 'string', ['string', 'in-route-untouched']],
            ['colayoutNetIntent', 'string', ['string', 'NET_M_A']],
        ],
        observed_net='NET_M_A',
        route_uid='r-in-keep',
    )
    in_del = _rect_obj('in-del', 200, 400, 800, 800, net='NET_M_A', name='IN_DEL')
    in_edit = _rect_obj('in-edit', 3200, 400, 3800, 800, net='NET_M_A', name='IN_EDIT')
    in_cancel = _rect_obj('in-cancel', 3200, 3200, 3800, 3600, net='NET_M_A', name='IN_CANCEL')
    snap = snap.model_copy(
        update={
            'objects': {
                **snap.objects,
                'in-route-untouched': in_route_keep,
                'in-del': in_del,
                'in-edit': in_edit,
                'in-cancel': in_cancel,
            },
            'routes': {
                'r-in-keep': NativeRoute(
                    uid='r-in-keep',
                    members=('in-route-untouched',),
                    status='normal',
                    net='NET_M_A',
                )
            },
            'opaque_counts': {**snap.opaque_counts, 'routes': 1},
        }
    )

    class SnapshotNative(FakeCoopNative):
        def __init__(self):
            super().__init__(tmp_path, snap, prof, circuit, connectivity)
            self.cells = {(snap.library, snap.cell): snap}
            self.captured_requests = []

        def read(self, library, cell):
            return self.cells[(library, cell)], self.profile

        def call(self, name, *values):
            if name == 'lcClonePaired':
                lib, src_cell, dst_cell, _seed = values
                src = self.cells[(lib, src_cell)]
                self.cells[(lib, dst_cell)] = src.model_copy(update={'cell': dst_cell, 'counter': '10'})
                self.calls.append((name, values))
                return ['copied', dst_cell]
            if name == 'ltRelease':
                return [1, 1, 0]
            return super().call(name, *values)

        def transact(self, library, cell, request):
            if (library, cell) == (snap.library, snap.cell):
                self.captured_requests.append(deepcopy(request))
            cv = self.cells[(library, cell)]
            saved_current = self.current
            self.current = cv
            try:
                guard_ops = [cmd for cmd in request['operations'] if cmd['action'] == 'guard']
                normal_req = {
                    **request,
                    'operations': [cmd for cmd in request['operations'] if cmd['action'] != 'guard'],
                }
                receipt = super().transact(library, cell, normal_req)
                if guard_ops:
                    objs = dict(self.current.objects)
                    for cmd in guard_ops:
                        guid = cmd['uid']
                        spec = cmd['guard']
                        pts = [[int(p[0]), int(p[1])] for p in spec['points']]
                        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                        master = Shape(
                            kind='path',
                            lpp=('M1', 'drawing'),
                            points=tuple(tuple(p) for p in pts),
                            width=200,
                            net_intent=spec.get('net_intent'),
                        )
                        gobj = Object(
                            uid=guid,
                            native_id=f'nat:{guid}',
                            kind='mpp',
                            bbox=((min(xs), min(ys)), (max(xs), max(ys))),
                            mpp=MultipartPath(
                                master=master,
                                name=spec['template'],
                                parts=[{'lpp': ['M1', 'drawing'], 'kind': 'rect', 'net_intent': spec.get('net_intent')}],
                                native_definition=spec['definition'],
                            ),
                            placement=Placement(origin=tuple(pts[0])),
                            attributes={'name': guid[:8]},
                            properties=[
                                ['colayoutObjectId', 'string', ['string', guid]],
                                ['colayoutStructureUid', 'string', ['string', guid]],
                                ['colayoutNetIntent', 'string', ['string', spec.get('net_intent') or '']],
                            ],
                            observed_net=spec.get('net_intent'),
                            group_uid=None,
                            read_only_reason=None,
                            deletion_allowed=True,
                        )
                        if cmd.get('placement'):
                            pl_data = dict(cmd['placement'])
                            pl_data['origin'] = [int(v) for v in pl_data['origin']]
                            gobj = placed(gobj, Placement.model_validate(pl_data))
                        objs[guid] = gobj
                    next_c = str(int(self.current.counter) + 1)
                    self.current = self.current.model_copy(update={'objects': objs, 'counter': next_c})
                self.cells[(library, cell)] = self.current
                if (library, cell) == (saved_current.library, saved_current.cell):
                    saved_current = self.current
                return receipt
            finally:
                self.current = saved_current

    fake = SnapshotNative()
    adapter = Adapter(fake)
    adapter.read = fake.read

    def fake_read_pair(lib, cell, *, schematic=None):
        s, p = fake.read(lib, cell)
        c = fake.circuit.model_copy(
            update={'source': fake.circuit.source.model_copy(update={'library': lib, 'cell': cell, 'counter': s.counter})}
        )
        conn = fake.connectivity.model_copy(
            update={'source': fake.connectivity.source.model_copy(update={'library': lib, 'cell': cell, 'counter': s.counter})}
        )
        return s, p, c, conn

    adapter.read_pair = fake_read_pair
    fake.read_pair = fake_read_pair

    editor = Editor(
        Workspace(
            baseline=snap,
            profile_id=prof.id,
            circuit=circuit,
            layout_connectivity=connectivity,
            connection_config=str(fake.config_path),
        ),
        prof,
    )
    a1 = RegionActions(editor).create((0, 0, 5000, 5000), 'Core_A')
    editor.scope_id = a1

    # Intermediate history inside Core_A: edit->delete, multi-edit, edit->revert, create->edit->delete, move->move, guard->move
    editor.edit('in-del', bbox=((200, 400), (900, 900)))
    editor.delete('in-del')

    editor.edit('in-edit', bbox=((3200, 400), (4200, 900)), net_intent='NET_TMP')
    editor.edit('in-edit', bbox=((3600, 1000), (4800, 2200)), net_intent='NET_FINAL')

    editor.edit('in-cancel', bbox=((3100, 3100), (3900, 3700)))
    editor.edit('in-cancel', bbox=((3200, 3200), (3800, 3600)))

    tmp_uid = editor.create(Shape(kind='rect', lpp=('M2', 'drawing'), bbox=((200, 3200), (900, 3800)), net_intent='TMP'))
    editor.edit(tmp_uid, bbox=((200, 3200), (1200, 3800)))
    editor.delete(tmp_uid)

    editor.place('inst-M_A', (3000, 1100), 'R90')
    editor.place('inst-M_A', (3500, 1200), 'MY')

    g1 = adapter.guard_preview(editor, bbox=((500, 3200), (1500, 4200)), template=prof.guard_templates[0], net_intent='NET_M_A')
    editor.place(g1, (2800, 3400), 'R90')

    editor.create(
        Shape(kind='rect', lpp=('M2', 'drawing'), bbox=((200, 1800), (1000, 2400)), net_intent='NET_M_A'),
        uid='final-ai',
    )

    committed = adapter.commit(editor.w, request_id='scheme-b-1')
    assert committed.state == 'clean'
    assert committed.operations == ()
    assert len(fake.captured_requests) == 1
    sent_ops = fake.captured_requests[0]['operations']

    # No historical 'before' or 'mpp_before' digests sent; net actions follow phase order without intermediate noise
    assert all('before' not in cmd and 'mpp_before' not in cmd for cmd in sent_ops)
    phase_order = {'delete': 0, 'delete_guard': 0, 'transform': 1, 'update': 2, 'guard': 3, 'create': 3}
    assert [phase_order[cmd['action']] for cmd in sent_ops] == sorted(
        phase_order[cmd['action']] for cmd in sent_ops
    )
    assert {(cmd['action'], cmd['uid']) for cmd in sent_ops} == {
        ('delete', 'in-del'),
        ('transform', 'inst-M_A'),
        ('update', 'in-edit'),
        ('create', 'final-ai'),
        ('guard', g1),
    }
    guard_cmd = next(cmd for cmd in sent_ops if cmd['uid'] == g1)
    assert guard_cmd['placement'] == {'origin': ['2800', '3400'], 'orientation': 'R90'}

    # Untouched region route shape + cancelled edit + reverted temp creation were never touched
    assert 'in-route-untouched' not in [cmd['uid'] for cmd in sent_ops]
    assert 'in-cancel' not in [cmd['uid'] for cmd in sent_ops]
    assert tmp_uid not in [cmd['uid'] for cmd in sent_ops]
    assert 'r-in-keep' in committed.baseline.routes
    assert committed.baseline.objects['in-edit'].shape.bbox == ((3600, 1000), (4800, 2200))
    assert committed.baseline.objects['in-edit'].shape.net_intent == 'NET_FINAL'
    assert committed.baseline.objects['inst-M_A'].placement == Placement(origin=(3500, 1200), orientation='MY')
    assert committed.baseline.objects[g1].placement == Placement(origin=(2800, 3400), orientation='R90')


def test_stale_operation_before_digest_still_syncs_and_pulls_from_final_snapshot(tmp_path):
    adapter, fake, editor, a1, _a2 = _make_adapter_rig(tmp_path)
    in_wire = _rect_obj('in-wire', 200, 3500, 1200, 4200, net='NET_M_A', name='IN_WIRE')
    snap_with_wire = fake.current.model_copy(update={'objects': {**fake.current.objects, 'in-wire': in_wire}})
    fake.current = snap_with_wire
    editor = Editor(
        editor.w.model_copy(
            update={
                'baseline': snap_with_wire,
                'local_data': editor.w.local_data.model_copy(
                    update={
                        'regions': {
                            **editor.w.local_data.regions,
                            a1: editor.w.local_data.regions[a1].model_copy(
                                update={'object_uids': (*editor.w.local_data.regions[a1].object_uids, 'in-wire')}
                            ),
                        }
                    }
                ),
            }
        ),
        editor.profile,
    )
    editor.scope_id = a1
    editor.edit('in-wire', bbox=((200, 3500), (1600, 4400)), net_intent='NET_NEW')
    editor.place('inst-M_A', (3200, 1200), 'R90')
    editor.create(
        Shape(kind='rect', lpp=('M2', 'drawing'), bbox=((3400, 3400), (4400, 4200)), net_intent='NET_M_A'),
        uid='in-added',
    )

    # Corrupt historical op.before digests to verify Scheme B ignores stale edit history
    stale_ops = tuple(
        op.model_copy(update={'before': 'stale-historical-digest'}) if op.before else op
        for op in editor.w.operations
    )
    stale_w = editor.w.model_copy(update={'operations': stale_ops})

    # 1) Fast-path commit (Virtuoso unchanged) succeeds without OBJECT_CONFLICT
    committed_fast = adapter.commit(stale_w, request_id='stale-fast-path')
    assert committed_fast.state == 'clean'
    assert fake.current.objects['in-wire'].shape.bbox == ((200, 3500), (1600, 4400))
    assert fake.current.objects['inst-M_A'].placement == Placement(origin=(3200, 1200), orientation='R90')
    assert 'in-added' in fake.current.objects

    # 2) Reset Virtuoso to snap_with_wire + outside Human edit -> cooperative_pull and commit both succeed!
    fake.current = snap_with_wire.model_copy(
        update={
            'objects': {
                **snap_with_wire.objects,
                'ext_edit': _rect_obj('ext_edit', 25000, 2000, 27500, 3000, net='EXT2', name='EXT_EDIT'),
            },
            'counter': '2',
        }
    )
    pulled, _ = adapter.cooperative_pull(stale_w)
    assert pulled.baseline.objects['ext_edit'].shape.bbox == ((25000, 2000), (27500, 3000))
    assert any(op.before for op in pulled.operations)
    assert all(op.before != 'stale-historical-digest' for op in pulled.operations if op.before)

    # Restore fake.current to post-pull state & commit stale_w directly on cooperative rebase path too!
    committed_coop = adapter.commit(stale_w, request_id='stale-coop-path')
    assert committed_coop.state == 'clean'
    assert committed_coop.baseline.objects['ext_edit'].shape.bbox == ((25000, 2000), (27500, 3000))
    assert committed_coop.baseline.objects['in-wire'].shape.bbox == ((200, 3500), (1600, 4400))
    assert committed_coop.baseline.objects['inst-M_A'].placement == Placement(origin=(3200, 1200), orientation='R90')
    assert 'in-added' in committed_coop.baseline.objects
