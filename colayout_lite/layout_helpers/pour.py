"""Net-intended metal pour with conservative obstacles and grid-safe clearance."""

from math import sqrt

from shapely import set_precision
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from ..actions import dbu
from ..drc.models import Region
from ..drc.scope import pin_context, selection
from ..materials import extract
from ..model import LiteError, Operation, Shape
from ..regions.constraints import PolicyGeometry
from ..regions.query import metals, object_net
from .common import direct_objects, report_bounds

MAX_POLYGONS = 512


def _polygons(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == 'Polygon':
        return [geometry] if geometry.area > 1e-9 else []
    if not hasattr(geometry, 'geoms'):
        return []
    return [part for child in geometry.geoms for part in _polygons(child)]


def _supported(geometry, allow_45):
    for polygon in _polygons(geometry):
        for ring in (polygon.exterior, *polygon.interiors):
            for a, b in zip(ring.coords, ring.coords[1:]):
                dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
                if dx > 1e-6 and dy > 1e-6 and (not allow_45 or abs(dx - dy) > 1e-6):
                    return False
    return True


def _orthogonal(geometry):
    return _supported(geometry, False)


def _orthogonal_cover(geometry, allow_45=False):
    covered = [
        polygon if _supported(polygon, allow_45) else box(*polygon.bounds)
        for polygon in _polygons(geometry)
    ]
    return unary_union(covered) if covered else geometry


def _points(polygon, grid, allow_45=False):
    if polygon.interiors or not polygon.is_valid or not _supported(polygon, allow_45):
        message = (
            '铺铜输出必须是只含水平、垂直或 45° 边的无孔多边形。'
            if allow_45 else '铺铜输出必须是只含水平/垂直边的无孔多边形。'
        )
        raise LiteError('POUR_NON_ORTHOGONAL_RESULT', message)
    points = []
    for x, y in polygon.exterior.coords[:-1]:
        point = (round(x), round(y))
        if any(abs(value - rounded) > 1e-6 for value, rounded in zip((x, y), point)):
            raise LiteError('POUR_GRID_SNAP_FAILED', '铺铜边界未能落到制造网格。')
        if any(value % grid for value in point):
            raise LiteError('POUR_GRID_SNAP_FAILED', '铺铜边界未能落到制造网格。')
        if not points or point != points[-1]:
            points.append(point)
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    while len(points) >= 3:
        simplified = []
        for index, point in enumerate(points):
            before = points[index - 1]
            after = points[(index + 1) % len(points)]
            if (point[0] - before[0]) * (after[1] - point[1]) != (point[1] - before[1]) * (after[0] - point[0]):
                simplified.append(point)
        if len(simplified) == len(points):
            break
        points = simplified
    edges = zip(points, points[1:] + points[:1])
    if any(
        a[0] != b[0] and a[1] != b[1]
        and (not allow_45 or abs(a[0] - b[0]) != abs(a[1] - b[1]))
        for a, b in edges
    ):
        raise LiteError('POUR_NON_ORTHOGONAL_RESULT', '铺铜输出包含不受支持角度的边。')
    return tuple(points)


def _vertical_slices(component, grid, allow_45=False):
    xs = sorted({
        round(x)
        for ring in (component.exterior, *component.interiors)
        for x, _ in ring.coords
    })
    min_y, max_y = component.bounds[1], component.bounds[3]
    slices = []
    for left, right in zip(xs, xs[1:]):
        if left == right:
            continue
        clipped = component.intersection(box(left, min_y - grid, right, max_y + grid))
        for part in _polygons(clipped):
            if part.interiors or not part.is_valid or not _supported(part, allow_45):
                raise LiteError('POUR_NON_ORTHOGONAL_RESULT', '铺铜垂直切片包含不受支持的角度或孔洞。')
            if not allow_45:
                rectangle = box(*part.bounds)
                if not part.equals(rectangle) or not _orthogonal(rectangle):
                    raise LiteError('POUR_NON_ORTHOGONAL_RESULT', '铺铜垂直切片未能形成矩形。')
                part = rectangle
            slices.append(part)
    if len(slices) > 2048:
        raise LiteError('POUR_TOO_COMPLEX', '铺铜中间切片超过 2048 个，请缩小范围或分区铺铜。')
    return slices


def _merge_slices(slices, allow_45=False):
    pieces = list(slices)
    merged = True
    while merged:
        merged = False
        for index, first in enumerate(pieces):
            for other in range(index + 1, len(pieces)):
                second = pieces[other]
                if first.boundary.intersection(second.boundary).length <= 1e-6:
                    continue
                combined = first.union(second)
                if (
                    combined.geom_type != 'Polygon' or combined.interiors or not combined.is_valid
                    or not _supported(combined, allow_45)
                ):
                    continue
                pieces[index] = combined
                del pieces[other]
                merged = True
                break
            if merged:
                break
    return pieces


def _pour_pieces(geometry, grid, allow_45=False):
    pieces = []
    slice_count = 0
    for component in _polygons(geometry):
        if not component.is_valid or not _supported(component, allow_45):
            raise LiteError('POUR_NON_ORTHOGONAL_RESULT', '铺铜计算结果包含不受支持的角度或无效多边形。')
        slices = _vertical_slices(component, grid, allow_45)
        merged = _merge_slices(slices, allow_45)
        if abs(sum(piece.area for piece in merged) - component.area) > 1e-4:
            raise LiteError('POUR_GEOMETRY_INVALID', '铺铜切片合并后面积不一致。')
        pieces.extend(merged)
        slice_count += len(slices)
    if len(pieces) > MAX_POLYGONS:
        raise LiteError(
            'POUR_TOO_COMPLEX',
            f'铺铜需要 {len(pieces)} 个多边形，超过单次 {MAX_POLYGONS} 个上限；请缩小范围或分区铺铜。',
            polygon_count=len(pieces),
            limit=MAX_POLYGONS,
        )
    return pieces, slice_count


def create(editor, args, rules=None, *, cancelled=None):
    profile = editor.profile
    if args.lpp not in profile.writable_lpps or args.lpp[0] not in metals(profile) or args.lpp[1] != 'drawing':
        raise LiteError('POUR_REQUIRES_WRITABLE_METAL', '铺铜层必须是已开放的金属 drawing LPP。')
    a, b, c, d = (dbu(value, profile) for value in args.bounds_um)
    if a >= c or b >= d:
        raise LiteError('INVALID_POUR_BOUNDS', '铺铜范围须为正的 [xmin,ymin,xmax,ymax]。')
    clearance = dbu(args.clearance, profile)
    if clearance <= 0:
        raise LiteError('INVALID_POUR_CLEARANCE', '铺铜间距必须大于零。')
    if rules is not None:
        required = max(
            (rule.value for rule in rules.rules if rule.enabled and rule.kind == 'spacing' and rule.layer == args.lpp[0]),
            default=0,
        )
        if clearance / profile.dbu_per_micron + 1e-12 < required:
            raise LiteError(
                'POUR_CLEARANCE_BELOW_RULE',
                f'该层已配置的本地间距规则要求至少 {required:g} µm。',
                required_um=required,
            )

    def checkpoint():
        if cancelled is not None and cancelled.is_set():
            raise LiteError('EDIT_CANCELLED', '铺铜未采用。')

    roi = box(a, b, c, d)
    region = editor.w.local_data.regions.get(editor.scope_id)
    if region and not box(*region.bbox[0], *region.bbox[1]).covers(roi):
        raise LiteError('POUR_OUTSIDE_REGION', '铺铜范围必须完全位于当前操作区内。')
    target_ids = list(args.targets or [])
    targets = direct_objects(editor, target_ids)
    unverified = []
    for uid, obj in targets.items():
        if obj.shape is None:
            raise LiteError('POUR_TARGET_TYPE_UNSUPPORTED', '连接目标只能是当前层的普通图形或过孔，不能是器件、MPP 或子操作区。', uid=uid)
        known_net = object_net(obj)
        if known_net is not None and known_net != args.net_intent:
            raise LiteError('POUR_TARGET_NET_CONFLICT', f'目标 {uid} 已标记为网络 {known_net}。', uid=uid, target_net=known_net)
        if known_net is None:
            unverified.append(uid)

    checkpoint()
    region_model = Region(bbox=((a, b), (c, d)))
    allowed, scope = selection(editor.w, region_model, editor.scope_id)
    halo = clearance + profile.grid
    physical, objects, warnings, _, context = extract(
        editor.w, profile, region_model, halo, checkpoint, owner_filter=allowed
    )
    physical += pin_context(editor.w, scope['native_instance_ids'], context, checkpoint)
    if warnings:
        raise LiteError('POUR_GEOMETRY_INCOMPLETE', '范围内存在不完整或不可用的几何，不能安全铺铜。', warnings=warnings)
    target_rows = [row for row in physical if row.layer == args.lpp[0] and row.owner in targets]
    missing_targets = sorted(set(target_ids) - {row.owner for row in target_rows})
    if missing_targets:
        raise LiteError('POUR_TARGET_LAYER_MISSING', '连接目标在铺铜范围内没有该金属层图形。', uids=missing_targets)

    policy = PolicyGeometry(editor.w, profile, objects)
    policy_rows, missing = policy.for_net(editor.scope_id, args.lpp[0], args.net_intent)
    if missing:
        raise LiteError('POUR_GEOMETRY_INCOMPLETE', '原生黑盒缺少边界，不能安全铺铜。', uids=missing)
    foreign = [row.geometry for row in physical if row.layer == args.lpp[0] and row.owner not in targets]
    obstacles = [
        _orthogonal_cover(geometry, args.allow_45)
        for geometry in foreign + [row[1] for row in policy_rows]
    ]
    target_geometry = unary_union([row.geometry for row in target_rows]) if target_rows else None
    grid_guard = sqrt(0.5) * profile.grid
    fill = None
    for attempt in range(4):
        checkpoint()
        padding = clearance + grid_guard + attempt * profile.grid
        blocked = unary_union([geometry.buffer(padding, join_style='mitre') for geometry in obstacles]) if obstacles else None
        candidate = roi.difference(blocked) if blocked is not None else roi
        candidate = set_precision(candidate, grid_size=profile.grid, mode='valid_output')
        if all(candidate.distance(geometry) + 1e-6 >= clearance for geometry in obstacles):
            fill = candidate
            break
    if fill is None:
        raise LiteError('POUR_CLEARANCE_UNPROVEN', '网格取整后无法证明实际间距，未生成铺铜。')
    if fill.is_empty or fill.area <= 1e-9:
        raise LiteError('POUR_EMPTY', '现有障碍和间距占满了铺铜范围。')

    discarded = []
    if target_geometry is not None:
        connected = []
        for component in _polygons(fill):
            if component.intersection(target_geometry).area > 1e-9:
                connected.append(component)
            else:
                discarded.append(component)
        if not connected:
            raise LiteError('POUR_NO_CONNECTED_AREA', '铺铜范围无法与任何指定目标形成有面积的同层连接。')
        fill = unary_union(connected)
    pieces, slice_count = _pour_pieces(fill, profile.grid, args.allow_45)
    operations = []
    try:
        for piece in pieces:
            shape = Shape(
                kind='polygon', lpp=tuple(args.lpp),
                points=_points(piece, profile.grid, args.allow_45), net_intent=args.net_intent,
            )
            operations.append(Operation(action='create', shape=shape))
    except ValueError as exc:
        raise LiteError('POUR_GEOMETRY_INVALID', f'铺铜拆分产生了无效多边形：{exc}') from exc
    if not operations:
        raise LiteError('POUR_EMPTY', '没有可生成的铺铜面积。')
    checkpoint()
    with editor.transaction() as staged:
        staged.apply(operations)
    created = [operation.uid for operation in operations]
    connected_targets = sorted(
        {row.owner for row in target_rows if any(piece.intersection(row.geometry).area > 1e-9 for piece in pieces)}
    )
    return {
        'created_uids': created,
        'polygon_count': len(created),
        'rectangle_slice_count': slice_count,
        'orthogonal': _orthogonal(fill),
        'area_um2': fill.area / profile.dbu_per_micron**2,
        'clearance_um': clearance / profile.dbu_per_micron,
        'grid_guard_um': grid_guard / profile.dbu_per_micron,
        'target_uids': target_ids,
        'connected_target_uids': connected_targets,
        'unverified_target_uids': unverified,
        'discarded_island_count': len(discarded),
        'discarded_island_area_um2': sum(piece.area for piece in discarded) / profile.dbu_per_micron**2,
        'verification_required': '生成只保证显式障碍间距与本地编辑策略；仍需运行本地及服务器 DRC/LVS。',
        **report_bounds(editor, created),
    }
