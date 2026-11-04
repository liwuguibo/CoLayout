"""Optional 45-degree metal pour preserves geometry and transaction invariants."""

import pytest
from shapely import set_precision
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from colayout_lite.editor import Editor, projected
from colayout_lite.layout_helpers.contracts import Pour
from colayout_lite.layout_helpers.service import execute
from colayout_lite.model import LiteError, Shape, Snapshot, Workspace
from colayout_lite.technology import TechnologyProfile

DIAMOND = ((10000, 4000), (16000, 10000), (10000, 16000), (4000, 10000))


@pytest.fixture
def profile():
    return TechnologyProfile(
        id='foreign', label='Synthetic technology', native_fingerprint='fp', dbu_per_micron=2000, grid=2,
        writable_lpps=(('wire_a', 'drawing'),),
        layers=[dict(layer='wire_a', purpose='drawing', material='metal')],
        vias={},
    )


@pytest.fixture
def editor(profile):
    snapshot = Snapshot(
        library='L', cell='C', library_path='/test/L', session='s', counter='1', dirty=False,
        dbu_per_micron=2000, grid=2, objects={}, technology_fingerprint='fp',
    )
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


def copper(editor, result):
    objects = projected(editor.w)
    return unary_union([Polygon(objects[uid].shape.points) for uid in result['created_uids']])


def assert_supported_output(editor, result, allow_45):
    objects = projected(editor.w)
    diagonal = False
    for uid in result['created_uids']:
        points = objects[uid].shape.points
        polygon = Polygon(points)
        assert polygon.is_valid and not polygon.interiors
        assert all(value % editor.profile.grid == 0 for point in points for value in point)
        for a, b in zip(points, points[1:] + points[:1]):
            dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
            assert dx == 0 or dy == 0 or (allow_45 and dx == dy)
            diagonal |= dx > 0 and dy > 0
    return diagonal


def expected_fill(obstacle):
    padding = 2000 + 2**0.5
    expected = box(0, 0, 20000, 20000).difference(obstacle.buffer(padding, join_style='mitre'))
    return set_precision(expected, grid_size=2, mode='valid_output')


def test_45_obstacle_and_hole_preserve_grid_area_spacing_and_atomic_undo(editor):
    obstacle = Polygon(DIAMOND)
    editor.create(Shape(kind='polygon', lpp=('wire_a', 'drawing'), points=DIAMOND, net_intent='VDD'))
    before = editor.w

    result = pour(editor, allow_45=True)
    generated = copper(editor, result)

    assert assert_supported_output(editor, result, allow_45=True)
    assert result['orthogonal'] is False
    assert result['rectangle_slice_count'] > result['polygon_count']
    assert generated.equals(expected_fill(obstacle))
    assert generated.area / editor.profile.dbu_per_micron**2 == pytest.approx(result['area_um2'])
    assert generated.distance(obstacle) >= 2000 - 1e-6
    editor.undo()
    assert editor.w.operations == before.operations


def test_default_and_explicit_false_keep_conservative_orthogonal_behavior(editor):
    obstacle = Polygon(DIAMOND)
    editor.create(Shape(kind='polygon', lpp=('wire_a', 'drawing'), points=DIAMOND, net_intent='VDD'))

    default_result = pour(editor)
    default_copper = copper(editor, default_result)
    assert not assert_supported_output(editor, default_result, allow_45=False)
    assert default_result['orthogonal'] is True
    assert default_copper.equals(expected_fill(box(*obstacle.bounds)))
    editor.undo()

    false_result = pour(editor, allow_45=False)
    assert copper(editor, false_result).equals(default_copper)
    assert false_result['area_um2'] == pytest.approx(default_result['area_um2'])
    assert not assert_supported_output(editor, false_result, allow_45=False)


def test_unsupported_obstacle_angle_uses_axis_aligned_cover_in_45_mode(editor):
    points = ((8000, 6000), (14000, 8000), (12000, 14000), (6000, 12000))
    obstacle = Polygon(points)
    editor.create(Shape(kind='polygon', lpp=('wire_a', 'drawing'), points=points, net_intent='VDD'))

    result = pour(editor, allow_45=True)
    generated = copper(editor, result)

    assert_supported_output(editor, result, allow_45=True)
    assert generated.equals(expected_fill(box(*obstacle.bounds)))
    assert generated.distance(obstacle) >= 2000 - 1e-6


def test_allow_45_contract_default_and_invalid_value_are_atomic(editor):
    params = {
        'bounds_um': [0, 0, 10, 10],
        'lpp': ['wire_a', 'drawing'],
        'net_intent': 'VSS',
        'clearance': 1,
    }
    assert Pour.model_validate(params).allow_45 is False
    assert Pour.model_validate({**params, 'allow_45': True}).allow_45 is True
    schema = Pour.model_json_schema()['properties']['allow_45']
    assert schema['type'] == 'boolean' and schema['default'] is False

    before = editor.w.model_dump_json()
    with pytest.raises(LiteError, match='INVALID_ARGUMENTS'):
        pour(editor, allow_45='sometimes')
    assert editor.w.model_dump_json() == before
