import pytest
from shapely.geometry import Polygon

from colayout_lite.model import Shape
from colayout_lite.parasitic_models import RcLateral, RcMetal, RcModel, RcOverlap, RcVia
from colayout_lite.parasitic_rc import (
    GeometryContribution,
    GeometrySet,
    RcBudget,
    analyze_geometry,
    compare_estimates,
    merge_geometry_sets,
    shape_geometry,
)
from colayout_lite.technology import TechnologyProfile


@pytest.fixture
def rc():
    return RcModel(
        source='synthetic unit model', corner='nominal', temperature_c=25,
        reference_environment='isolated conductors over reference ground; no neighbouring conductors',
        metals={'wire_a': RcMetal(sheet_resistance_ohm_per_sq=.1, area_cap_fF_per_um2=2,
            perimeter_cap_fF_per_um=.5, min_width_um=.05, max_width_um=1),
                'wire_b': RcMetal(sheet_resistance_ohm_per_sq=.2, area_cap_fF_per_um2=1,
            perimeter_cap_fF_per_um=.25, min_width_um=.05, max_width_um=1)},
        vias={'JOIN': RcVia(resistance_ohm_per_cut=6)},
        lateral={'wire_a': RcLateral(coefficient_fF_per_um=.2, min_spacing_um=.05, max_spacing_um=2)},
        overlap={'wire_a|wire_b': RcOverlap(coefficient_fF_per_um2=.3)},
    )


@pytest.fixture
def profile(rc):
    return TechnologyProfile(
        id='synthetic', label='Synthetic RC', native_fingerprint='fp', dbu_per_micron=2000, grid=2,
        writable_lpps=(('wire_a', 'drawing'), ('wire_b', 'drawing')),
        layers=[dict(layer=name, purpose='drawing', material='metal') for name in ('wire_a', 'wire_b')],
        vias={'JOIN': dict(kind='stdViaDef', layer1='wire_a', layer2='wire_b', cut_layer='cut_x', implant1=None, implant2=None,
            defaults=dict(cutWidth=200, cutHeight=240, cutRows=1, cutColumns=1, cutSpacing=[200, 220], layer1Enc=[80, 100],
                layer2Enc=[100, 120], layer1Offset=[0, 0], layer2Offset=[0, 0], originOffset=[0, 0]))},
        parasitics=rc,
    )


def path(uid='wire', y=0, width=2000, layer='wire_a'):
    return Shape(kind='path', lpp=(layer, 'drawing'), points=((0, y), (20000, y)), width=width)


def test_simple_path_resistance_ground_capacitance_and_via_parallel_cuts(profile):
    result = analyze_geometry(shape_geometry(path(), profile, 'wire'), GeometrySet(), profile)
    assert result.resistance_ohm == pytest.approx(1)
    assert result.ground_capacitance_fF == pytest.approx(31)
    assert result.coupling_capacitance_fF == 0
    via = Shape(kind='stdVia', via_definition='JOIN', origin=(0, 0), via_parameters={'cutRows': 2, 'cutColumns': 2})
    via_result = analyze_geometry(shape_geometry(via, profile, 'via'), GeometrySet(), profile)
    assert via_result.resistance_ohm == pytest.approx(1.5)


def test_parallel_coupling_is_symmetric_and_unknown_net_is_not_different(profile):
    first = shape_geometry(path('a'), profile, 'a', net='A')
    second = shape_geometry(path('b', y=4000), profile, 'b', net='B')
    result = analyze_geometry(first, second, profile)
    reverse = analyze_geometry(second, first, profile)
    assert result.coupling_capacitance_fF == pytest.approx(2)
    assert reverse.coupling_capacitance_fF == pytest.approx(result.coupling_capacitance_fF)
    assert result.impacts[0].relation == 'different_net'
    unknown = analyze_geometry(first, shape_geometry(path('c', y=4000), profile, 'c'), profile)
    assert unknown.impacts[0].relation == 'unknown_net'


def test_same_physical_component_has_no_internal_coupling_even_across_layers(profile):
    first = shape_geometry(path('a'), profile, 'a', component_id='same')
    other = Shape(kind='rect', lpp=('wire_b', 'drawing'), bbox=((0, -1000), (20000, 1000)))
    second = shape_geometry(other, profile, 'b', component_id='same')
    result = analyze_geometry(first, second, profile)
    assert result.coupling_capacitance_fF == 0 and not result.impacts


def test_ground_capacitance_unions_connected_geometry_before_area_and_perimeter(profile):
    left = Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((0, 0), (10000, 2000)))
    right = Shape(kind='rect', lpp=('wire_a', 'drawing'), bbox=((10000, 0), (20000, 2000)))
    rows = merge_geometry_sets(shape_geometry(left, profile, 'left', component_id='same'),
                               shape_geometry(right, profile, 'right', component_id='same'))
    result = analyze_geometry(rows, GeometrySet(), profile)
    assert result.ground_capacitance_fF == pytest.approx(31)
    assert result.resistance_status == 'unavailable'


def test_width_outside_model_range_is_not_silently_extrapolated(profile):
    result = analyze_geometry(shape_geometry(path(width=4000), profile, 'wide'), GeometrySet(), profile)
    assert result.resistance_status == 'out_of_model_range'
    assert result.ground_status == 'out_of_model_range'
    assert any('width' in warning for warning in result.warnings)


def test_missing_overlap_model_and_incomplete_results_cannot_be_compared(profile, rc):
    rc.overlap.clear()
    first = shape_geometry(path('a'), profile, 'a')
    other = Shape(kind='rect', lpp=('wire_b', 'drawing'), bbox=((0, -1000), (20000, 1000)))
    second = shape_geometry(other, profile, 'b')
    result = analyze_geometry(first, second, profile)
    assert result.coupling_status == 'partial'
    assert result.impacts[0].status == 'missing_model'
    assert compare_estimates(result, result) is None


def test_complex_contour_is_rejected_before_large_geometric_operation(profile):
    points = [(i, 0 if i % 2 else 2000) for i in range(100)] + [(0, 0)]
    contribution = GeometryContribution('complex', 'complex', 'wire_a', Polygon(points), 'complex')
    result = analyze_geometry(GeometrySet((contribution,), simple_series=False), GeometrySet(), profile,
                              budget=RcBudget(max_polygon_vertices=20, max_operation_vertices=20))
    assert result.ground_status == 'partial'
    assert result.coupling_status == 'partial'
    assert any('vertices' in warning for warning in result.warnings)


def test_deleted_neighbor_contribution_is_negative_by_stable_source_group(profile):
    before = analyze_geometry(shape_geometry(path('a'), profile, 'a'),
                              shape_geometry(path('b', y=4000), profile, 'b'), profile)
    after = analyze_geometry(GeometrySet(), shape_geometry(path('b', y=4000), profile, 'b'), profile)
    delta = compare_estimates(before, after)
    assert delta['delta_cc_fF'] == pytest.approx(-2)
    assert len(delta['impact_deltas']) == 1
    assert delta['impact_deltas'][0]['delta_fF'] == pytest.approx(-2)


def test_split_and_merge_use_group_totals_not_component_identity(profile):
    neighbor = shape_geometry(path('n', y=4000), profile, 'n')
    whole = analyze_geometry(shape_geometry(path('whole'), profile, 'whole'), neighbor, profile)
    left = Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, 0), (10000, 0)), width=2000)
    right = Shape(kind='path', lpp=('wire_a', 'drawing'), points=((10000, 0), (20000, 0)), width=2000)
    split = merge_geometry_sets(shape_geometry(left, profile, 'left'), shape_geometry(right, profile, 'right'))
    after = analyze_geometry(split, neighbor, profile)
    delta = compare_estimates(whole, after)
    assert delta['delta_cc_fF'] == pytest.approx(0)
    assert sum(row['delta_fF'] for row in delta['impact_deltas']) == pytest.approx(0)
    joined = merge_geometry_sets(shape_geometry(left, profile, 'left', component_id='joined'),
                                 shape_geometry(right, profile, 'right', component_id='joined'))
    connected = analyze_geometry(joined, neighbor, profile)
    assert compare_estimates(after, connected)['delta_cc_fF'] == pytest.approx(0)


def test_resistance_only_model_never_fabricates_capacitance(profile):
    profile = profile.model_copy(update={'parasitics': profile.parasitics.model_copy(
        update={'capacitance_status': 'unconfigured', 'coverage_notes': ['capacitance not configured']})})
    result = analyze_geometry(shape_geometry(path(), profile, 'wire'), GeometrySet(), profile)
    assert result.resistance_ohm == 1
    assert result.ground_status == result.coupling_status == 'unconfigured'
    assert result.ground_capacitance_fF == result.coupling_capacitance_fF == 0
    assert result.warnings == ('capacitance not configured',)
