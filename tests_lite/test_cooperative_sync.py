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
from colayout_lite.editor import Editor, projected
from colayout_lite.model import (
    LiteError,
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
            assert (lib, cell) == (self.current.library, self.current.cell)
            if self.current.dirty:
                return ['drawn_unsaved', len(guides), self.current.counter]
            self.guides = deepcopy(guides)
            next_c = str(int(self.current.counter) + 1)
            self.current = self.current.model_copy(update={'counter': next_c})
            return ['synced', len(guides), next_c]
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
                            update={'shape': s, 'bbox': s.bbox, 'observed_net': s.net_intent}
                        )
                elif act == 'delete':
                    objs.pop(uid)
                elif act == 'transform':
                    pl = Placement.model_validate(cmd['placement'])
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

