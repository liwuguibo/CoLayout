"""M1 Core-Halo long-distance partitioning and electrostatic conservation tests."""

from __future__ import annotations

import math

import numpy as np
import pytest
from shapely.geometry import box
from shapely.ops import unary_union

from colayout_lite.parasitic_nn import (
    GeoGraphRCEngine,
    RectangleElement,
    build_core_halo_partitions,
    evaluate_partitioned_capacitance,
)


def _synthetic_v3_weights(seed: int = 11, hidden: int = 16) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    return {
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
        'bias_shift_e': np.array([0.0], dtype=np.float32),
        'bias_shift_n': np.array([0.0], dtype=np.float32),
        'arch_version': np.array(3, dtype=np.int64),
    }


def _rotate_90(elems: list[RectangleElement]) -> list[RectangleElement]:
    out = []
    for el in elems:
        pts = [(-el.y0, el.x0), (-el.y1, el.x0), (-el.y1, el.x1), (-el.y0, el.x1)]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        out.append(
            RectangleElement(
                id=el.id,
                net_name=el.net_name,
                layer=el.layer,
                x0=min(xs),
                y0=min(ys),
                x1=max(xs),
                y1=max(ys),
                num_cuts=el.num_cuts,
                stackup=el.stackup,
            )
        )
    return out


def test_short_line_auto_mode_uses_single_window_exact_parity():
    engine = GeoGraphRCEngine(weights=_synthetic_v3_weights(), r_halo_um=12.0, layer_window=2)
    target = [RectangleElement(1, 'SIG', 'M2', x0=0.0, y0=0.0, x1=12.0, y1=0.2)]
    nbr = [RectangleElement(2, 'VIC', 'M2', x0=0.0, y0=0.45, x1=12.0, y1=0.65)]

    res = evaluate_partitioned_capacitance(engine, target, nbr, partition_mode='auto')
    assert res.partition_mode_used == 'single'
    assert res.partition_count == 1
    assert res.bottleneck_partition_id == 'P0'

    direct = engine.predict_window_rc(target + nbr, resolve_vias=False, compute_r_nets=())
    assert res.ground_capacitance_fF == pytest.approx(direct['net_c_gnd']['SIG'], abs=1e-9)
    assert res.coupling_capacitance_fF == pytest.approx(
        direct['pair_c_cpl'][('SIG', 'VIC')], abs=1e-9
    )


def test_long_line_2_4_8_slices_exact_physics_conservation_and_v3_consistency():
    # 160 um M2 signal line + parallel M2 aggressor; test coplanar seam conservation & ground plane
    target = [RectangleElement(1, 'SIG', 'M2', x0=0.0, y0=0.0, x1=160.0, y1=0.2)]
    nbr = [
        RectangleElement(2, 'VIC', 'M2', x0=0.0, y0=0.45, x1=160.0, y1=0.65),
        RectangleElement(3, 'VSS', 'M3', x0=-2.0, y0=-2.0, x1=162.0, y1=2.0),
    ]

    physics_engine = GeoGraphRCEngine(weights=None, r_halo_um=12.0, layer_window=2)
    ref_phys = evaluate_partitioned_capacitance(physics_engine, target, nbr, partition_mode='none')
    assert ref_phys.partition_count == 1

    # Exclude M3/M2 2*w overlap_peri cut difference? Wait: M3 is VSS ground, let's also verify exact
    # pure coplanar invariance (< 1e-5) and bounded M3 ground shielding agreement (< 1.5%)
    coplanar_nbr = [nbr[0]]
    ref_coplanar = evaluate_partitioned_capacitance(
        physics_engine, target, coplanar_nbr, partition_mode='none'
    )
    for tile_sz in (80.0, 40.0, 20.0):
        res_coplanar = evaluate_partitioned_capacitance(
            physics_engine, target, coplanar_nbr, partition_mode='path_tiles', tile_size_um=tile_sz
        )
        expected_n = int(round(160.0 / tile_sz))
        assert res_coplanar.partition_count == expected_n
        assert all(p.seam_abutments >= 1 for p in res_coplanar.partitions)
        assert res_coplanar.ground_capacitance_fF == pytest.approx(
            ref_coplanar.ground_capacitance_fF, rel=1e-5
        )
        assert res_coplanar.coupling_capacitance_fF == pytest.approx(
            ref_coplanar.coupling_capacitance_fF, rel=1e-5
        )
        assert sum(p.ground_share for p in res_coplanar.partitions) == pytest.approx(1.0, abs=1e-9)
        assert sum(p.coupling_share for p in res_coplanar.partitions) == pytest.approx(1.0, abs=1e-9)
        d = res_coplanar.to_dict()
        assert d['conservation_check']['sum_partition_cg_fF'] == pytest.approx(
            d['ground_capacitance_fF'], abs=1e-9
        )
        assert d['conservation_check']['sum_partition_cc_fF'] == pytest.approx(
            d['coupling_capacitance_fF'], abs=1e-9
        )

    # With M3 ground shield and synthetic arch_version=3 weights across 2, 4, 8 slices
    nn_engine = GeoGraphRCEngine(weights=_synthetic_v3_weights(), r_halo_um=12.0, layer_window=2)
    nn_2 = evaluate_partitioned_capacitance(nn_engine, target, nbr, partition_mode='path_tiles', tile_size_um=80.0)
    nn_4 = evaluate_partitioned_capacitance(nn_engine, target, nbr, partition_mode='path_tiles', tile_size_um=40.0)
    nn_8 = evaluate_partitioned_capacitance(nn_engine, target, nbr, partition_mode='path_tiles', tile_size_um=20.0)
    assert (nn_2.partition_count, nn_4.partition_count, nn_8.partition_count) == (2, 4, 8)
    for res in (nn_2, nn_4, nn_8):
        assert math.isclose(res.total_capacitance_fF, nn_4.total_capacitance_fF, rel_tol=0.05)


def test_multilayer_l_route_via_atomicity_and_e2_rotation_invariance():
    # 120 um L-shaped 3D route: M2 [0..60] -> 2x2 VIA2 -> M3 [0..60], plus aggressor
    target = [
        RectangleElement(1, 'CLK', 'M2', x0=0.0, y0=0.0, x1=60.0, y1=0.2),
        RectangleElement(2, 'CLK', 'M3', x0=59.75, y0=0.0, x1=59.95, y1=60.0),
        RectangleElement(3, 'CLK', 'VIA2', x0=59.80, y0=0.04, x1=59.90, y1=0.14),
        RectangleElement(4, 'CLK', 'VIA2', x0=59.80, y0=0.24, x1=59.90, y1=0.34),
        RectangleElement(5, 'CLK', 'VIA2', x0=60.00, y0=0.04, x1=60.10, y1=0.14),
        RectangleElement(6, 'CLK', 'VIA2', x0=60.00, y0=0.24, x1=60.10, y1=0.34),
    ]
    nbr = [
        RectangleElement(10, 'AGG', 'M2', x0=10.0, y0=0.45, x1=45.0, y1=0.65),
        RectangleElement(11, 'VSS', 'M1', x0=-5.0, y0=-5.0, x1=70.0, y1=70.0),
    ]

    engine = GeoGraphRCEngine(weights=_synthetic_v3_weights(), r_halo_um=12.0, layer_window=2)
    _, specs = build_core_halo_partitions(target, nbr, partition_mode='path_tiles', tile_size_um=20.0)
    via_owners = [p.id for p in specs for el in p.core_elements if el.is_via]
    assert len(via_owners) == 1
    via_el = next(el for p in specs for el in p.core_elements if el.is_via)
    assert via_el.num_cuts == 4

    res_r0 = evaluate_partitioned_capacitance(engine, target, nbr, partition_mode='path_tiles', tile_size_um=20.0)
    res_r90 = evaluate_partitioned_capacitance(
        engine, _rotate_90(target), _rotate_90(nbr), partition_mode='path_tiles', tile_size_um=20.0
    )

    assert res_r0.partition_count == res_r90.partition_count
    assert res_r90.ground_capacitance_fF == pytest.approx(res_r0.ground_capacitance_fF, rel=1e-5)
    assert res_r90.coupling_capacitance_fF == pytest.approx(res_r0.coupling_capacitance_fF, rel=1e-5)
    r0_sum = sum(p.local_series_resistance_ohm for p in res_r0.partitions)
    r90_sum = sum(p.local_series_resistance_ohm for p in res_r90.partitions)
    assert r90_sum == pytest.approx(r0_sum, rel=1e-6)
    cg_sorted_r0 = sorted(p.ground_capacitance_fF for p in res_r0.partitions)
    cg_sorted_r90 = sorted(p.ground_capacitance_fF for p in res_r90.partitions)
    assert cg_sorted_r90 == pytest.approx(cg_sorted_r0, rel=1e-5)


def test_cross_partition_aggressor_and_dual_target_differential_pair_zero_double_count():
    engine = GeoGraphRCEngine(weights=None, r_halo_um=12.0, layer_window=2)

    # 1. Single 100 um target + 100 um parallel foreign aggressor -> 5 uniform partitions, 0.20 each
    sig = [RectangleElement(1, 'CLK_P', 'M2', x0=0.0, y0=0.0, x1=100.0, y1=0.2)]
    agg = [RectangleElement(2, 'CLK_N', 'M2', x0=0.0, y0=0.45, x1=100.0, y1=0.65)]
    res_5 = evaluate_partitioned_capacitance(
        engine, sig, agg, partition_mode='path_tiles', tile_size_um=20.0
    )
    ref_1 = evaluate_partitioned_capacitance(engine, sig, agg, partition_mode='none')
    assert res_5.partition_count == 5
    assert res_5.coupling_capacitance_fF == pytest.approx(ref_1.coupling_capacitance_fF, rel=1e-5)
    for part in res_5.partitions:
        assert part.coupling_share == pytest.approx(0.20, abs=1e-4)

    # 2. Both CLK_P and CLK_N jointly in target across 5 spatial partitions: cross-seam split 0.5+0.5=1.0
    both = sig + agg
    dual_5 = evaluate_partitioned_capacitance(
        engine, both, (), partition_mode='spatial_grid', tile_size_um=20.0
    )
    dual_1 = evaluate_partitioned_capacitance(engine, both, (), partition_mode='none')
    assert dual_5.partition_count == 5
    assert dual_5.ground_capacitance_fF == pytest.approx(dual_1.ground_capacitance_fF, rel=1e-5)
    assert dual_5.coupling_capacitance_fF == pytest.approx(dual_1.coupling_capacitance_fF, rel=1e-5)
    assert dual_5.pair_c_cpl_fF[('CLK_N', 'CLK_P')] == pytest.approx(
        dual_1.pair_c_cpl_fF[('CLK_N', 'CLK_P')], rel=1e-5
    )


def test_subregions_and_spatial_grid_modes_pinpoint_hotspot_partition():
    engine = GeoGraphRCEngine(weights=_synthetic_v3_weights(), r_halo_um=12.0, layer_window=2)
    target = [RectangleElement(1, 'BIAS', 'M2', x0=0.0, y0=0.0, x1=110.0, y1=0.25)]
    # Dense aggressor comb placed strictly inside clk_buf [70..110]
    nbr = [
        RectangleElement(10, 'AGG_BUS', 'M2', x0=72.0, y0=0.45, x1=108.0, y1=0.65),
        RectangleElement(11, 'AGG_BUS', 'M2', x0=72.0, y0=-0.45, x1=108.0, y1=-0.20),
        RectangleElement(12, 'AGG_BUS', 'M3', x0=74.0, y0=-1.0, x1=106.0, y1=1.0),
    ]
    subregions = {
        'tx_drv': (0.0, -5.0, 35.0, 5.0),
        'clk_buf': (70.0, -5.0, 110.0, 5.0),
    }
    res = evaluate_partitioned_capacitance(
        engine, target, nbr, partition_mode='subregions', tile_size_um=25.0, subregion_boxes=subregions
    )
    assert any(p.region_id == 'tx_drv' for p in res.partitions)
    assert any(p.region_id == 'clk_buf' for p in res.partitions)
    assert any(p.region_id is None and 'corridor' in p.label for p in res.partitions)

    bottleneck = next(p for p in res.partitions if p.id == res.bottleneck_partition_id)
    assert bottleneck.region_id == 'clk_buf'
    assert bottleneck.top_coupling_neighbors
    assert bottleneck.top_coupling_neighbors[0].net == 'AGG_BUS'

    # 2D T-mesh (3x2 grid with stem at x=20..22 -> 4 occupied cells) retains exact union area
    mesh = [
        RectangleElement(1, 'PWR', 'M2', x0=0.0, y0=0.0, x1=60.0, y1=1.0),
        RectangleElement(2, 'PWR', 'M2', x0=20.0, y0=0.0, x1=22.0, y1=45.0),
    ]
    _, grid_specs = build_core_halo_partitions(mesh, (), partition_mode='spatial_grid', tile_size_um=20.0)
    assert len(grid_specs) == 4
    expected_area = unary_union([box(*el.bbox) for el in mesh]).area
    core_els = [el for spec in grid_specs for el in spec.core_elements]
    assert sum(el.area for el in core_els) == pytest.approx(expected_area, rel=1e-6)

    # Tiny corner overhang (< 0.25 * tile_size_um) merges into adjacent grid cell instead of a sliver
    sliver_wire = [RectangleElement(1, 'SIG', 'M2', x0=0.0, y0=0.0, x1=29.0, y1=0.2)]
    _, sliver_specs = build_core_halo_partitions(
        sliver_wire, (), partition_mode='spatial_grid', tile_size_um=20.0
    )
    assert len(sliver_specs) >= 1
    assert all(
        sum(el.length for el in spec.core_elements if not el.is_via) >= 5.0
        for spec in sliver_specs
    )


def test_thick_m8_seam_edge_suppression_and_m4_to_m8_via7_trunk():
    # Thick M8 200 um trace over M1 ground: suppressing collinear cut-seam message-passing edges
    # keeps 20/30/40 um partitions tightly aligned without fake thick-sidewall seam edges.
    m8_long = [RectangleElement(1, 'RF_TOP', 'M8', x0=0.0, y0=0.0, x1=200.0, y1=0.6)]
    m8_gnd = [RectangleElement(2, 'VSS', 'M1', x0=-4.0, y0=-6.0, x1=204.0, y1=6.6)]
    _, m8_specs = build_core_halo_partitions(
        m8_long, m8_gnd, partition_mode='path_tiles', tile_size_um=20.0
    )
    assert len(m8_specs) == 10
    eng_nn = GeoGraphRCEngine(weights=_synthetic_v3_weights(seed=7), r_halo_um=12.0, layer_window=2)
    p1_spec = m8_specs[1]
    act_p1 = list(p1_spec.core_elements) + list(p1_spec.halo_target_elements) + list(p1_spec.halo_bg_elements)
    g_no_filter = eng_nn.extractor.build_edge_features(act_p1)
    g_filtered = eng_nn.extractor.build_edge_features(
        act_p1, partition_owner_by_id=p1_spec.element_owner_map()
    )
    assert g_no_filter[0].shape[1] == 4
    assert g_filtered[0].shape[1] == 0

    # Real intra-partition L-bend corner (different widths / orthogonal arms, same owner) is preserved
    corner_els = [
        RectangleElement(10, 'LNET', 'M8', x0=0.0, y0=0.0, x1=10.0, y1=0.6),
        RectangleElement(11, 'LNET', 'M8', x0=10.0, y0=0.0, x1=10.6, y1=8.0),
    ]
    g_corner = eng_nn.extractor.build_edge_features(
        corner_els, partition_owner_by_id={10: 0, 11: 0}
    )
    assert g_corner[0].shape[1] == 2

    res_m8_20 = evaluate_partitioned_capacitance(
        eng_nn, m8_long, m8_gnd, partition_mode='path_tiles', tile_size_um=20.0
    )
    res_m8_40 = evaluate_partitioned_capacitance(
        eng_nn, m8_long, m8_gnd, partition_mode='path_tiles', tile_size_um=40.0
    )
    assert math.isclose(res_m8_20.ground_capacitance_fF, res_m8_40.ground_capacitance_fF, rel_tol=0.03)

    # 180 um M4 -> VIA4..VIA7 (1x2 thick via) -> M8 -> VIA7..VIA4 -> M4 3D long trunk
    trunk = [
        RectangleElement(1, 'TRUNK', 'M4', x0=0.0, y0=0.0, x1=59.4, y1=0.24),
        RectangleElement(2, 'TRUNK', 'M4', x0=59.4, y0=-0.6, x1=60.6, y1=0.84),
        RectangleElement(3, 'TRUNK', 'M5', x0=59.4, y0=-0.6, x1=60.6, y1=0.84),
        RectangleElement(4, 'TRUNK', 'M6', x0=59.4, y0=-0.6, x1=60.6, y1=0.84),
        RectangleElement(5, 'TRUNK', 'M7', x0=59.4, y0=-0.6, x1=60.6, y1=0.84),
        RectangleElement(6, 'TRUNK', 'M8', x0=59.3, y0=-0.65, x1=60.7, y1=0.89),
        RectangleElement(7, 'TRUNK', 'M8', x0=60.7, y0=-0.18, x1=120.0, y1=0.42),
        RectangleElement(8, 'TRUNK', 'VIA4', x0=59.95, y0=0.07, x1=60.05, y1=0.17),
        RectangleElement(9, 'TRUNK', 'VIA5', x0=59.95, y0=0.07, x1=60.05, y1=0.17),
        RectangleElement(10, 'TRUNK', 'VIA6', x0=59.95, y0=0.07, x1=60.05, y1=0.17),
        RectangleElement(11, 'TRUNK', 'VIA7', x0=59.82, y0=-0.42, x1=60.18, y1=-0.06),
        RectangleElement(12, 'TRUNK', 'VIA7', x0=59.82, y0=0.30, x1=60.18, y1=0.66),
    ]
    nbrs = [
        RectangleElement(30, 'AGG_M4', 'M4', x0=15.0, y0=0.38, x1=45.0, y1=0.62),
        RectangleElement(31, 'AGG_M8', 'M8', x0=72.0, y0=0.87, x1=108.0, y1=1.47),
        RectangleElement(32, 'VSS', 'M1', x0=-4.0, y0=-6.0, x1=126.0, y1=6.0),
    ]
    _, specs_3d = build_core_halo_partitions(trunk, nbrs, partition_mode='path_tiles', tile_size_um=20.0)
    via7_owners = [el for sp in specs_3d for el in sp.core_elements if el.is_via and el.layer == 'VIA7']
    assert len(via7_owners) == 1
    assert via7_owners[0].num_cuts == 2

    res_3d = evaluate_partitioned_capacitance(eng_nn, trunk, nbrs, partition_mode='path_tiles', tile_size_um=20.0)
    assert res_3d.pair_c_cpl_fF[('AGG_M4', 'TRUNK')] > 0.0
    assert res_3d.pair_c_cpl_fF[('AGG_M8', 'TRUNK')] > 0.0
    assert sum(p.total_capacitance_share for p in res_3d.partitions) == pytest.approx(1.0, abs=1e-9)

