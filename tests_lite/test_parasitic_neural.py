# ruff: noqa: F811
import hashlib
import math

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from colayout_lite.circuit.models import LayoutConnectivity, Terminal, ViewRef
from colayout_lite.model import Object, Placement, Shape, Snapshot, Workspace
from colayout_lite.parasitic_models import (
    RcModel,
    RcSurrogateDielectricZone,
    RcSurrogateModel,
    RcSurrogateStackupLayer,
    RcSurrogateViaSpec,
)
from colayout_lite.parasitic_nn import (
    AnalyticalResistorEngine,
    GeoGraphRCEngine,
    LayoutGraphExtractor,
    RectangleElement,
    resolve_layout_connectivity,
)
from colayout_lite.parasitic_rc import (
    RcBudget,
    analyze_geometry,
    compare_estimates,
    shape_geometry,
)
from colayout_lite.parasitic_workspace import analyze_workspace
from colayout_lite.process_measurements import policy_key
from colayout_lite.process_package import ProcessPackage, portable
from colayout_lite.technology import TechnologyProfile
from colayout_lite.wire_capture import DraftRoute
from tests_lite.test_parasitic_rc import profile, rc  # noqa: F401
from tests_lite.test_parasitic_view import app, host_window, wait_until  # noqa: F401
from tests_lite.test_parasitic_workspace import blackbox_workspace, guard_blackbox


def _synthetic_weights(seed: int = 7, hidden: int = 32, arch_version: int | None = 3):
    rng = np.random.default_rng(seed)
    weights = {
        'v_mean': np.zeros((22,), dtype=np.float32),
        'v_std': np.ones((22,), dtype=np.float32),
        'e_mean': np.zeros((22,), dtype=np.float32),
        'e_std': np.ones((22,), dtype=np.float32),
        'W_v0': rng.normal(0.0, 0.05, size=(22, hidden)).astype(np.float32),
        'b_v0': np.zeros((hidden,), dtype=np.float32),
        'W_e0': rng.normal(0.0, 0.05, size=(22, hidden)).astype(np.float32),
        'b_e0': np.zeros((hidden,), dtype=np.float32),
        'W_em1': rng.normal(0.0, 0.04, size=(hidden, hidden)).astype(np.float32),
        'W_eh1': rng.normal(0.0, 0.04, size=(hidden, hidden)).astype(np.float32),
        'b_e1': np.zeros((hidden,), dtype=np.float32),
        'W_nh1': rng.normal(0.0, 0.04, size=(hidden, hidden)).astype(np.float32),
        'W_na1': rng.normal(0.0, 0.04, size=(hidden, hidden)).astype(np.float32),
        'b_n1': np.zeros((hidden,), dtype=np.float32),
        'W_em2': rng.normal(0.0, 0.04, size=(hidden, hidden)).astype(np.float32),
        'W_eh2': rng.normal(0.0, 0.04, size=(hidden, hidden)).astype(np.float32),
        'b_e2': np.zeros((hidden,), dtype=np.float32),
        'W_nh2': rng.normal(0.0, 0.04, size=(hidden, hidden)).astype(np.float32),
        'W_na2': rng.normal(0.0, 0.04, size=(hidden, hidden)).astype(np.float32),
        'b_n2': np.zeros((hidden,), dtype=np.float32),
        'W_er1': rng.normal(0.0, 0.05, size=(hidden, hidden)).astype(np.float32),
        'b_er1': np.zeros((hidden,), dtype=np.float32),
        'W_er2': rng.normal(0.0, 0.03, size=(hidden, 2)).astype(np.float32),
        'b_er2': np.zeros((2,), dtype=np.float32),
        'W_nr1': rng.normal(0.0, 0.05, size=(hidden, hidden)).astype(np.float32),
        'b_nr1': np.zeros((hidden,), dtype=np.float32),
        'W_nr2': rng.normal(0.0, 0.03, size=(hidden, 2)).astype(np.float32),
        'b_nr2': np.zeros((2,), dtype=np.float32),
        'bias_shift_e': np.array([0.55], dtype=np.float32),
        'bias_shift_n': np.array([0.55], dtype=np.float32),
    }
    if arch_version is not None:
        weights['arch_version'] = np.array(arch_version, dtype=np.int64)
    return weights


def _write_weights(tmp_path, *, arch_version: int | None = 3):
    tag = f'v{arch_version}' if arch_version is not None else 'v2'
    path = tmp_path / f'synthetic_geograph_{tag}.npz'
    np.savez(path, **_synthetic_weights(arch_version=arch_version))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, sha


def _transform_rects(elems, tx: float = 0.0, ty: float = 0.0, rot90: bool = False, mirror_x: bool = False):
    out = []
    for el in elems:
        pts = [(el.x0, el.y0), (el.x1, el.y0), (el.x1, el.y1), (el.x0, el.y1)]
        if rot90:
            pts = [(-y, x) for x, y in pts]
        if mirror_x:
            pts = [(-x, y) for x, y in pts]
        pts = [(x + tx, y + ty) for x, y in pts]
        out.append(RectangleElement(
            id=el.id,
            net_name=el.net_name,
            layer=el.layer,
            x0=min(p[0] for p in pts),
            y0=min(p[1] for p in pts),
            x1=max(p[0] for p in pts),
            y1=max(p[1] for p in pts),
            num_cuts=el.num_cuts,
            stackup=el.stackup,
        ))
    return out


def test_geograph_e2_invariance(tmp_path):
    wpath, sha = _write_weights(tmp_path)
    engine = GeoGraphRCEngine(weights_path=wpath, expected_sha256=sha)
    base = [
        RectangleElement(1, 'SIG', 'M1', x0=0.0, y0=0.0, x1=4.0, y1=0.2),
        RectangleElement(2, 'AGR', 'M1', x0=0.0, y0=0.4, x1=3.0, y1=0.6),
        RectangleElement(3, 'SIG', 'VIA1', x0=3.7, y0=0.05, x1=3.8, y1=0.15),
        RectangleElement(4, 'SIG', 'M2', x0=3.6, y0=0.0, x1=3.9, y1=2.5),
        RectangleElement(5, 'VSS', 'M1', x0=0.0, y0=-0.6, x1=4.0, y1=-0.4),
    ]
    ref = engine.predict_window_rc(base)
    for variant in (
        _transform_rects(base, tx=17.5, ty=-9.25),
        _transform_rects(base, rot90=True),
        _transform_rects(base, mirror_x=True),
        _transform_rects(base, tx=3.0, ty=4.0, rot90=True, mirror_x=True),
    ):
        res = engine.predict_window_rc(variant)
        assert res['net_c_tot']['SIG'] == pytest.approx(ref['net_c_tot']['SIG'], rel=1e-5, abs=1e-6)
        assert res['net_c_gnd']['SIG'] == pytest.approx(ref['net_c_gnd']['SIG'], rel=1e-5, abs=1e-6)
        assert res['net_r_ohm']['SIG'] == pytest.approx(ref['net_r_ohm']['SIG'], rel=1e-5, abs=1e-6)
        for key, val in ref['pair_c_cpl'].items():
            assert res['pair_c_cpl'][key] == pytest.approx(val, rel=1e-5, abs=1e-6)


def test_via_drop_merges_nets_eliminates_coupling_and_reduces_r(tmp_path):
    wpath, _ = _write_weights(tmp_path)
    engine = GeoGraphRCEngine(weights_path=wpath)
    existing = [
        RectangleElement(1, 'NET_A', 'M1', x0=0.0, y0=0.0, x1=4.0, y1=0.2),
        RectangleElement(2, 'NET_B', 'M2', x0=3.6, y0=0.0, x1=3.9, y1=4.0),
        RectangleElement(3, 'VICTIM', 'M2', x0=4.2, y0=0.0, x1=4.4, y1=4.0),
    ]
    v1 = RectangleElement(4, 'VIA_AUTO', 'VIA1', x0=3.7, y0=0.05, x1=3.8, y1=0.15)
    inc = engine.predict_incremental_rc(existing, v1)
    assert inc['self_net']['topology_merged'] is True
    assert inc['self_net']['merged_from_nets'] == ['NET_A', 'NET_B']
    assert inc['self_net']['eliminated_interlayer_c_cpl_ff'] > 0.0
    assert inc['self_net']['r_after_ohm'] > 0.0

    with_v1 = existing + [v1]
    v2 = RectangleElement(5, 'VIA_AUTO', 'VIA1', x0=3.7, y0=0.05, x1=3.8, y1=0.15)
    v2_shifted = RectangleElement(5, 'VIA_AUTO', 'VIA1', x0=3.62, y0=0.05, x1=3.72, y1=0.15)
    assert v2.bbox != v2_shifted.bbox
    inc_parallel = engine.predict_incremental_rc(with_v1, v2_shifted)
    assert inc_parallel['self_net']['topology_merged'] is False
    assert inc_parallel['self_net']['delta_r_ohm'] < 0.0


def test_nodal_resistance_and_via_clustering():
    elems = [
        RectangleElement(1, 'S', 'M1', x0=0.0, y0=0.0, x1=2.0, y1=0.2),
        RectangleElement(2, 'S', 'M2', x0=1.7, y0=0.0, x1=3.7, y1=0.2),
        RectangleElement(3, 'S', 'VIA1', x0=1.75, y0=0.05, x1=1.85, y1=0.15),
        RectangleElement(4, 'S', 'VIA1', x0=1.75, y0=0.05, x1=1.85, y1=0.15),
    ]
    conn_single = resolve_layout_connectivity(elems[:3])
    conn_double = resolve_layout_connectivity([elems[0], elems[1], elems[2], RectangleElement(4, 'S', 'VIA1', x0=1.87, y0=0.05, x1=1.97, y1=0.15)])
    r1 = AnalyticalResistorEngine.calc_net_elements_r(conn_single.clustered_elements, 'S')
    r2 = AnalyticalResistorEngine.calc_net_elements_r(conn_double.clustered_elements, 'S')
    assert r1 > r2 > 0.0


def test_weights_sha256_check_and_process_portability(tmp_path, profile):
    wpath, sha = _write_weights(tmp_path)
    bad_sha = '0' * 64
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        GeoGraphRCEngine(weights_path=wpath, expected_sha256=bad_sha)

    surr = RcSurrogateModel(
        kind='geograph_rc_v3',
        preset='synthetic_demo',
        weights_path=str(wpath),
        weights_sha256=sha,
        layer_map={'wire_a': 'M1', 'wire_b': 'M2'},
        via_map={'JOIN': 'VIA1'},
    )
    rc_model = RcModel(
        source='synthetic test',
        corner='tt',
        temperature_c=25.0,
        reference_environment='ideal substrate',
        surrogate=surr,
    )
    updated = profile.model_copy(update={'parasitics': rc_model})
    assert updated.rc_model_digest != profile.rc_model_digest
    assert policy_key(updated) == policy_key(profile)

    pkg = portable(ProcessPackage(technology=updated))
    assert pkg.technology.parasitics.surrogate.weights_path is None
    assert pkg.technology.parasitics.surrogate.weights_sha256 == sha


def test_technology_profile_rejects_unknown_surrogate_mapping(profile):
    bad = RcModel(
        source='synthetic test',
        corner='tt',
        temperature_c=25.0,
        reference_environment='ideal substrate',
        surrogate=RcSurrogateModel(
            preset='synthetic_demo',
            layer_map={'UNKNOWN_METAL': 'M1'},
        ),
    )
    with pytest.raises(ValueError, match='unknown conductor layer'):
        TechnologyProfile.model_validate({**profile.model_dump(mode='json'), 'parasitics': bad.model_dump(mode='json')})


def test_custom_stackup_and_analyze_geometry_with_bend_and_coupling(tmp_path, profile):
    wpath, sha = _write_weights(tmp_path)
    surr_rc = RcModel(
        source='synthetic test',
        corner='tt',
        temperature_c=25.0,
        reference_environment='ideal substrate',
        surrogate=RcSurrogateModel(
            kind='geograph_rc_v3',
            preset='custom',
            weights_path=str(wpath),
            weights_sha256=sha,
            stackup_layers={
                'M1': RcSurrogateStackupLayer(z_min_um=0.30, z_max_um=0.50, rsh_ohm_per_sq=0.10),
                'M2': RcSurrogateStackupLayer(z_min_um=0.70, z_max_um=0.90, rsh_ohm_per_sq=0.10),
            },
            stackup_vias={
                'VIA1': RcSurrogateViaSpec(
                    bot_layer='M1', top_layer='M2',
                    z_min_um=0.50, z_max_um=0.70,
                    cut_width_um=0.2, min_space_um=0.2,
                    r_single_ohm=1.0,
                ),
            },
            dielectric_zones=(RcSurrogateDielectricZone(z_min_um=0.0, z_max_um=3.0, eps_r=3.5),),
            via_map={'M2_M1': 'VIA1'},
        ),
    )
    prof = profile.model_copy(update={'parasitics': surr_rc})

    l_wire = Shape(kind='path', lpp=('M1', 'drawing'), points=((0, 0), (3000, 0), (3000, 3000)), width=200)
    neighbor = Shape(kind='rect', lpp=('M1', 'drawing'), bbox=((0, 400), (3000, 600)))
    target = shape_geometry(l_wire, prof, 'w1', net='SIG_A', simple_series=True)
    aggressor = shape_geometry(neighbor, prof, 'w2', net='SIG_B', simple_series=True)
    est = analyze_geometry(target, aggressor, prof, budget=RcBudget(analysis_radius_um=5.0))
    assert est.mode == 'geograph_rc_v3'
    assert est.comparable
    assert est.resistance_ohm is not None and math.isfinite(est.resistance_ohm) and est.resistance_ohm > 0
    assert est.ground_capacitance_fF > 0
    assert est.coupling_capacitance_fF > 0
    assert len(est.impacts) == 1
    assert est.impacts[0].relation == 'different_net'


def test_workspace_draft_via_bridge_and_blackbox_safety(tmp_path, profile, monkeypatch):
    guard_blackbox(monkeypatch)
    wpath, sha = _write_weights(tmp_path)
    surr_rc = RcModel(
        source='synthetic test',
        corner='tt',
        temperature_c=25.0,
        reference_environment='ideal substrate',
        surrogate=RcSurrogateModel(
            kind='geograph_rc_v3',
            preset='synthetic_demo',
            weights_path=str(wpath),
            weights_sha256=sha,
            layer_map={'wire_a': 'M1', 'wire_b': 'M2'},
            via_map={'JOIN': 'VIA1'},
        ),
    )
    prof = profile.model_copy(update={'parasitics': surr_rc})
    ws = blackbox_workspace(prof)

    # Without selecting blackbox internals, its boundary PINs participate while internals remain hidden.
    est_base, graph = analyze_workspace(ws, prof, None, ['wire'])
    assert est_base.mode == 'geograph_rc_v3'
    assert 'bb' in graph['blackbox_ids']
    assert all(n['owner_uid'] != 'bb_secret' for n in graph['nodes'])

    # Dropping a DraftRoute spanning wire_a -> JOIN -> wire_b computes neural RC & compares delta cleanly.
    draft = DraftRoute(
        points=((0, 0), (2000, 0), (2000, 2000)),
        layers=('wire_a', 'wire_b'),
        widths_um=(0.2, 0.2),
        via_definitions=('JOIN',),
    )
    est_draft, _ = analyze_workspace(
        ws,
        prof,
        None,
        ['wire'],
        extra_target=draft.geometry(prof),
        exclude_uids=('wire',),
    )
    assert est_draft.mode == 'geograph_rc_v3'
    assert est_draft.resistance_ohm is not None and est_draft.resistance_ohm > 1.5
    delta = compare_estimates(est_base, est_draft)
    assert delta is not None


def test_parasitic_view_runs_neural_surrogate(app, tmp_path, profile, monkeypatch):  # noqa: F811
    wpath, sha = _write_weights(tmp_path)
    surr_rc = RcModel(
        source='synthetic test',
        corner='tt',
        temperature_c=25.0,
        reference_environment='ideal substrate',
        surrogate=RcSurrogateModel(
            kind='geograph_rc_v3',
            preset='synthetic_demo',
            weights_path=str(wpath),
            weights_sha256=sha,
            layer_map={'wire_a': 'M1', 'wire_b': 'M2'},
            via_map={'JOIN': 'VIA1'},
        ),
    )
    prof = profile.model_copy(update={'parasitics': surr_rc})
    host = host_window(prof, monkeypatch)
    try:
        assert wait_until(app, lambda: host.parasitics.estimate is not None)
        assert host.parasitics.estimate.mode == 'geograph_rc_v3'
        assert '神经网络估算' in host.parasitics.panel.status.text()
    finally:
        host.parasitics.close()
        host.close()
        QApplication.processEvents()


def test_v2_legacy_weights_remain_supported(tmp_path, profile):
    wpath, sha = _write_weights(tmp_path, arch_version=None)
    surr_rc = RcModel(
        source='synthetic test',
        corner='tt',
        temperature_c=25.0,
        reference_environment='ideal substrate',
        surrogate=RcSurrogateModel(
            kind='geograph_rc_v2',
            preset='synthetic_demo',
            weights_path=str(wpath),
            weights_sha256=sha,
            layer_map={'wire_a': 'M1', 'wire_b': 'M2'},
            via_map={'JOIN': 'VIA1'},
        ),
    )
    prof = profile.model_copy(update={'parasitics': surr_rc})
    wire = Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, 0), (4000, 0)), width=400)
    neighbor = Shape(kind='path', lpp=('wire_b', 'drawing'), points=((2000, -1000), (2000, 1000)), width=400)
    est = analyze_geometry(
        shape_geometry(wire, prof, 'w1', net='A'),
        shape_geometry(neighbor, prof, 'w2', net='B'),
        prof,
    )
    assert est.mode == 'geograph_rc_v2'
    assert est.comparable and est.ground_capacitance_fF > 0 and est.coupling_capacitance_fF > 0


def test_v3_five_nanometer_e2_invariance_and_l_via_wall_splitting(tmp_path):
    wpath, sha = _write_weights(tmp_path, arch_version=3)
    engine = GeoGraphRCEngine(weights_path=wpath, expected_sha256=sha)
    # 5nm grid-aligned shapes + L-shaped via wall along M1/M2 corner (unioned, then split by _split_noncompact_group).
    elems = [
        RectangleElement(1, 'SIG', 'M1', x0=0.005, y0=0.005, x1=2.005, y1=0.105),
        RectangleElement(2, 'SIG', 'M1', x0=2.005, y0=0.005, x1=2.105, y1=2.005),
    ]
    next_id = 3
    for k in range(11):
        cx = 0.205 + k * 0.300
        elems.append(RectangleElement(next_id, 'WALL', 'VIA1', x0=cx, y0=2.405, x1=cx + 0.100, y1=2.505))
        next_id += 1
    for k in range(10):
        cy = 2.405 + k * 0.300
        elems.append(RectangleElement(next_id, 'WALL', 'VIA1', x0=3.755, y0=cy, x1=3.855, y1=cy + 0.100))
        next_id += 1

    conn = resolve_layout_connectivity(elems)
    wall_clusters = [el for el in conn.clustered_elements if el.is_via and el.net_name == 'WALL']
    assert len(wall_clusters) >= 2

    ref = engine.predict_window_rc(elems)
    for variant in (
        _transform_rects(elems, tx=-9.115, ty=4.335),
        _transform_rects(elems, rot90=True),
        _transform_rects(elems, mirror_x=True),
        _transform_rects(elems, tx=2.015, ty=-1.105, rot90=True, mirror_x=True),
    ):
        res = engine.predict_window_rc(variant)
        assert res['net_c_tot']['SIG'] == pytest.approx(ref['net_c_tot']['SIG'], rel=1e-5, abs=1e-6)
        assert res['net_c_tot']['WALL'] == pytest.approx(ref['net_c_tot']['WALL'], rel=1e-5, abs=1e-6)


def test_v3_shared_abutting_edge_deduction_preserves_ground_cap():
    extractor = LayoutGraphExtractor()
    whole = [RectangleElement(1, 'S', 'M2', x0=0.0, y0=0.0, x1=4.0, y1=0.5)]
    split = [
        RectangleElement(1, 'S', 'M2', x0=0.0, y0=0.0, x1=2.0, y1=0.5),
        RectangleElement(2, 'S', 'M2', x0=2.0, y0=0.0, x1=4.0, y1=0.5),
    ]
    conn_split = resolve_layout_connectivity(split)
    assert len({el.net_name for el in conn_split.clustered_elements}) == 1
    assert abs(split[0].x1 - split[1].x0) < 1e-6

    f_whole = extractor.build_node_features(whole, whole)
    f_split = extractor.build_node_features(split, split)
    cg_whole = float(f_whole[0, 16] + f_whole[0, 17])
    cg_split = float((f_split[:, 16] + f_split[:, 17]).sum())
    assert cg_split == pytest.approx(cg_whole, rel=1e-4, abs=1e-5)


def test_v3_comb_finger_blocking_and_bounded_dense_readout(tmp_path):
    wpath, sha = _write_weights(tmp_path, arch_version=3)
    engine = GeoGraphRCEngine(weights_path=wpath, expected_sha256=sha)
    # 80 interdigitated thin fingers on M3 (0.10um width, 0.10um spacing).
    dense = [
        RectangleElement(
            i + 1,
            'NET_A' if i % 2 == 0 else 'NET_B',
            'M3',
            x0=0.0,
            y0=i * 0.200,
            x1=10.0,
            y1=i * 0.200 + 0.100,
        )
        for i in range(80)
    ]
    res = engine.predict_window_rc(dense)
    assert math.isfinite(res['net_c_tot']['NET_A']) and res['net_c_tot']['NET_A'] > 0
    assert math.isfinite(res['net_c_tot']['NET_B']) and res['net_c_tot']['NET_B'] > 0

    # 1D LOS comb blocking prunes non-adjacent coplanar edges between fingers i and j separated by blockers.
    e_idx, _ = engine.extractor.build_edge_features(dense[:6])
    pairs = {(int(e_idx[0, k]), int(e_idx[1, k])) for k in range(e_idx.shape[1])}
    assert (0, 1) in pairs
    assert (0, 3) not in pairs


def test_workspace_via_without_net_intent_inherits_connected_ground_pin(tmp_path, profile):
    wpath, sha = _write_weights(tmp_path, arch_version=3)
    surr_rc = RcModel(
        source='synthetic test',
        corner='tt',
        temperature_c=25.0,
        reference_environment='ideal substrate',
        surrogate=RcSurrogateModel(
            kind='geograph_rc_v3',
            preset='synthetic_demo',
            weights_path=str(wpath),
            weights_sha256=sha,
            layer_map={'wire_a': 'M1', 'wire_b': 'M2'},
            via_map={'JOIN': 'VIA1'},
        ),
    )
    prof = profile.model_copy(update={'parasitics': surr_rc})

    node = [None] * 12
    node[10] = []
    node[11] = []
    root = [None] * 12
    root[4] = 1
    root[5] = [[-5000, -5000], [25000, 10000]]
    root[9] = []
    root[10] = []
    root[11] = []
    gnd_pin_rect = Object(
        uid='gnd_pad',
        native_id='gnd_pad',
        kind='rect',
        shape=Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((0, 2000), (6000, 4000)), net_intent='VSS'),
    )
    floating_via = Object(
        uid='via_gnd',
        native_id='via_gnd',
        kind='stdVia',
        shape=Shape(kind='stdVia', via_definition='JOIN', origin=(2000, 3000), net_intent=None),
    )
    sig_wire = Object(
        uid='sig',
        native_id='sig',
        kind='path',
        shape=Shape(kind='path', lpp=('wire_b', 'drawing'), points=((0, 6000), (10000, 6000)), width=1000, net_intent='SIG'),
    )
    snapshot = Snapshot(
        library='L',
        cell='C',
        library_path='/test/L',
        session='s',
        counter='1',
        dirty=False,
        dbu_per_micron=2000,
        grid=2,
        objects={'gnd_pad': gnd_pin_rect, 'via_gnd': floating_via, 'sig': sig_wire},
        pins=[],
        terminals=[
            ['VSS', 'inputOutput', 'VSS', [['pin0', None, ['gnd_pad']]]],
            ['SIG', 'inputOutput', 'SIG', [['pin0', None, ['sig']]]],
        ],
        display={'root': 'root', 'nodes': {'root': root}, 'styles': []},
        technology_fingerprint='fp',
    )
    connectivity = LayoutConnectivity(
        source=ViewRef(library='L', cell='C', view='layout', session='s', counter='1'),
        instances={},
        ports={'VSS': Terminal(name='VSS', net='VSS'), 'SIG': Terminal(name='SIG', net='SIG')},
    )
    ws = Workspace(baseline=snapshot, profile_id=prof.id, layout_connectivity=connectivity)
    est, _ = analyze_workspace(ws, prof, None, ['sig'])
    assert est.mode == 'geograph_rc_v3'
    assert est.comparable
    # Since gnd_pad + via_gnd both resolve to VSS, neither creates a different-net coupling impact against SIG.
    assert all('via_gnd' not in imp.neighbor_component for imp in est.impacts)
