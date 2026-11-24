"""Domain rules for decoupled Virtuoso <-> CoLayout cooperative synchronization.

Supports three-way spatial merging when Human edits in Virtuoso and AI/Human edits
in CoLayout occupy disjoint regions, rejects Virtuoso edits overlapping any
OperationRegion with actionable object/coordinate diagnostics, and builds
non-fabricating native Virtuoso region warning guide specifications.
"""

from __future__ import annotations

from typing import Any

from .circuit.query import rebind_layout
from .editor import intent_properties, projected
from .groups import require_groups
from .hierarchy import retain_display, same_dependencies
from .local_models import require_references
from .model import LiteError, Object, Operation, Placement, Snapshot, Workspace, digest
from .mpp import placed
from .regions.constraints import validate_pending, validate_workspace
from .regions.models import enclosing_scope, overlaps
from .regions.query import descendants, object_bounds, owner_map
from .routing import require_persistent_routes
from .sync_checks import canonical_object, require_unchanged, same_snapshot

_MODELED_OPAQUE_COUNTS = frozenset({'mosaics', 'figGroups', 'routes'})


def region_guide_signature(w: Workspace) -> tuple:
    """Stable signature of OperationRegion frames drawn as Virtuoso warning guides."""
    return tuple(
        (rid, region.name, region.bbox)
        for rid, region in sorted(w.local_data.regions.items())
    )


def ascii_guide_label(region) -> str:
    """ASCII warning banner suitable for Virtuoso X11 stick labels."""
    name = (region.name or '').strip()
    short_id = region.id[:12]
    if name and all(32 <= ord(ch) < 127 and ch not in ('"', '\\') for ch in name):
        return f'[CoLayout AI Region: {name} ({short_id})] DO NOT EDIT INSIDE'
    return f'[CoLayout AI Region: {short_id}] DO NOT EDIT INSIDE'


def build_region_guides(w: Workspace) -> list[dict]:
    """Encode local OperationRegions as DBU guide specs for ltSyncRegionGuides."""
    guides = []
    for rid, region in sorted(w.local_data.regions.items()):
        guides.append(
            {
                'id': rid,
                'name': region.name,
                'label': ascii_guide_label(region),
                'bbox': [
                    [str(region.bbox[0][0]), str(region.bbox[0][1])],
                    [str(region.bbox[1][0]), str(region.bbox[1][1])],
                ],
            }
        )
    return guides


def require_virtuoso_saved(snapshot: Snapshot) -> None:
    """Reject cooperative sync while Virtuoso holds unsaved GUI modifications."""
    if snapshot.dirty:
        raise LiteError(
            'VIRTUOSO_UNSAVED_CHANGES',
            'Virtuoso 中存在未保存的版图修改（modifiedButNotSaved=t）。'
            '请先在 Virtuoso 中保存（F2）或放弃未保存更改后再同步，以防止双方修改被覆盖。',
            library=snapshot.library,
            cell=snapshot.cell,
            retryable=True,
        )


def needs_native_prepare(snapshot: Snapshot) -> bool:
    """Return True when Virtuoso has new/copied objects or routes missing persistent UIDs."""
    for uid, obj in snapshot.objects.items():
        if uid.startswith('session:') or obj.read_only_reason in (
            'missing_persistent_identity',
            'duplicate_persistent_identity',
        ):
            return True
    try:
        require_persistent_routes(snapshot)
        require_groups(snapshot)
    except LiteError:
        return True
    return False


def translate_native_unsaved_error(exc: LiteError) -> LiteError:
    """Map low-level SKILL VIRTUOSO_UNSAVED_CHANGES errors to a structured LiteError."""
    if exc.code == 'NATIVE_ERROR' and 'VIRTUOSO_UNSAVED_CHANGES' in str(exc):
        return LiteError(
            'VIRTUOSO_UNSAVED_CHANGES',
            'Virtuoso 中存在未保存的版图修改（modifiedButNotSaved=t）。'
            '请先在 Virtuoso 中保存（F2）或放弃未保存更改后再同步。',
            retryable=True,
        )
    return exc


def _union_bounds(boxes) -> tuple[tuple[int, int], tuple[int, int]] | None:
    valid = [b for b in boxes if b is not None]
    if not valid:
        return None
    return (
        (min(b[0][0] for b in valid), min(b[0][1] for b in valid)),
        (max(b[1][0] for b in valid), max(b[1][1] for b in valid)),
    )


def _has_structural_conflict(w: Workspace, current: Snapshot) -> bool:
    if (
        current.technology_fingerprint != w.baseline.technology_fingerprint
        or current.dbu_per_micron != w.baseline.dbu_per_micron
        or current.grid != w.baseline.grid
    ):
        return True
    if not same_dependencies(w.baseline, current):
        return True
    base_unmodeled = {
        k: v for k, v in w.baseline.opaque_counts.items() if k not in _MODELED_OPAQUE_COUNTS
    }
    curr_unmodeled = {
        k: v for k, v in current.opaque_counts.items() if k not in _MODELED_OPAQUE_COUNTS
    }
    if base_unmodeled != curr_unmodeled:
        return True
    base_mosaics = sum(1 for o in w.baseline.objects.values() if o.kind == 'mosaic')
    curr_mosaics = sum(1 for o in current.objects.values() if o.kind == 'mosaic')
    if (
        current.opaque_counts.get('mosaics', 0) - w.baseline.opaque_counts.get('mosaics', 0)
        != curr_mosaics - base_mosaics
    ):
        return True
    if (current.routes is None) != (w.baseline.routes is None):
        return True
    if current.routes is not None and (
        current.opaque_counts.get('routes', 0) - w.baseline.opaque_counts.get('routes', 0)
        != len(current.routes) - len(w.baseline.routes)
    ):
        return True
    if (current.fig_groups is None) != (w.baseline.fig_groups is None):
        return True
    if current.fig_groups is not None and (
        current.opaque_counts.get('figGroups', 0) - w.baseline.opaque_counts.get('figGroups', 0)
        != len(current.fig_groups) - len(w.baseline.fig_groups)
    ):
        return True
    for term in current.terminals or ():
        for pin_row in term[3] or ():
            if 'unresolved' in (pin_row[2] or ()):
                return True
    return False


def _object_lpp(obj) -> list[str] | None:
    if obj and obj.shape and obj.shape.lpp:
        return list(obj.shape.lpp)
    if obj and obj.kind == 'stdVia' and obj.shape:
        return [obj.shape.via_definition]
    return None


def _object_label(obj, uid: str) -> str:
    if obj is None:
        return uid[:12]
    return str(obj.attributes.get('name') or uid[:16])


def _bounds_um(bounds, scale: int) -> list[float] | None:
    if bounds is None:
        return None
    return [round(bounds[0][0] / scale, 4), round(bounds[0][1] / scale, 4), round(bounds[1][0] / scale, 4), round(bounds[1][1] / scale, 4)]


def _format_intrusion_error(intrusions: list[dict]) -> LiteError:
    action_zh = {
        'added': 'Virtuoso 新增',
        'modified': 'Virtuoso 修改',
        'deleted': 'Virtuoso 删除',
        'route_member': 'Virtuoso 布线修改涉及区内对象',
    }
    lines = []
    for row in intrusions[:10]:
        desc = f"{action_zh.get(row['change'], row['change'])}: {row['name']} [{row['kind']}]"
        if row.get('lpp'):
            desc += f" 层={'/'.join(row['lpp'])}"
        if row.get('bounds_um'):
            b = row['bounds_um']
            desc += f" ({b[0]:.3f}, {b[1]:.3f})–({b[2]:.3f}, {b[3]:.3f}) µm"
        desc += f" → 落入操作区「{row['region_name']}」({row['region_id'][:12]})"
        lines.append('· ' + desc)
    if len(intrusions) > 10:
        lines.append(f'· 另有 {len(intrusions) - 10} 处重叠项。')
    message = (
        f'检测到 Virtuoso 中的版图改动落入了 CoLayout 操作区内（共 {len(intrusions)} 处），已阻止合并以保护双方工作：\n'
        + '\n'.join(lines)
        + '\n请在 Virtuoso 中删除或移出上述落在操作区内的元素并保存（F2）后重试同步。当前 CoLayout 草稿与 Virtuoso 修改均完整保留。'
    )
    return LiteError('VIRTUOSO_EDIT_IN_REGION', message, intrusions=intrusions, retryable=True)


def analyze_virtuoso_delta(w: Workspace, current: Snapshot, profile) -> dict:
    """Diff current Virtuoso snapshot against w.baseline and reject region intrusions."""
    require_virtuoso_saved(current)
    if _has_structural_conflict(w, current):
        require_unchanged(w, current)

    old_objs = w.baseline.objects
    new_objs = current.objects
    old_text = w.baseline.text_owners
    new_text = current.text_owners

    added = sorted(uid for uid in new_objs.keys() - old_objs.keys() if uid not in new_text)
    deleted = sorted(uid for uid in old_objs.keys() - new_objs.keys() if uid not in old_text)
    modified = sorted(
        uid
        for uid in (old_objs.keys() & new_objs.keys())
        if uid not in old_text
        and uid not in new_text
        and canonical_object(old_objs[uid]) != canonical_object(new_objs[uid])
    )

    scale = profile.dbu_per_micron
    base_only = w.model_copy(update={'operations': (), 'guard_previews': {}})
    curr_only = Workspace(baseline=current, profile_id=profile.id)
    regions = w.local_data.regions
    region_members = {uid: rid for rid, region in regions.items() for uid in region.object_uids}

    old_bounds_map: dict[str, tuple[tuple[int, int], tuple[int, int]] | None] = {}
    new_bounds_map: dict[str, tuple[tuple[int, int], tuple[int, int]] | None] = {}
    intrusions: list[dict] = []

    for change, uids in (('added', added), ('modified', modified), ('deleted', deleted)):
        for uid in uids:
            old_obj = old_objs.get(uid)
            new_obj = new_objs.get(uid)
            old_b = object_bounds(base_only, old_obj, profile) if old_obj is not None else None
            new_b = object_bounds(curr_only, new_obj, profile) if new_obj is not None else None
            if old_b is not None:
                old_bounds_map[uid] = old_b
            if new_b is not None:
                new_bounds_map[uid] = new_b
            ref_obj = new_obj or old_obj
            for rid, region in regions.items():
                member_hit = region_members.get(uid) == rid
                geom_hit = (old_b is not None and overlaps(region.bbox, old_b)) or (
                    new_b is not None and overlaps(region.bbox, new_b)
                )
                if member_hit or geom_hit:
                    intrusions.append(
                        {
                            'uid': uid,
                            'change': change,
                            'kind': ref_obj.kind if ref_obj else 'unknown',
                            'name': _object_label(ref_obj, uid),
                            'lpp': _object_lpp(new_obj) or _object_lpp(old_obj),
                            'bounds_um': _bounds_um(new_b or old_b, scale),
                            'old_bounds_um': _bounds_um(old_b, scale),
                            'region_id': rid,
                            'region_name': region.name,
                            'region_bounds_um': _bounds_um(region.bbox, scale),
                        }
                    )

    if regions and (w.baseline.routes is not None or current.routes is not None):
        base_routes = w.baseline.routes or {}
        curr_routes = current.routes or {}
        changed_route_ids = [
            rid
            for rid in base_routes.keys() | curr_routes.keys()
            if rid not in base_routes
            or rid not in curr_routes
            or base_routes[rid].semantic() != curr_routes[rid].semantic()
        ]
        reported = {(row['uid'], row['region_id']) for row in intrusions}
        for ruid in sorted(changed_route_ids):
            for route in (base_routes.get(ruid), curr_routes.get(ruid)):
                if route is None:
                    continue
                for member_uid in route.members:
                    reg_id = region_members.get(member_uid)
                    if reg_id and (member_uid, reg_id) not in reported:
                        reported.add((member_uid, reg_id))
                        ref_obj = new_objs.get(member_uid) or old_objs.get(member_uid)
                        region = regions[reg_id]
                        b = new_bounds_map.get(member_uid) or old_bounds_map.get(member_uid)
                        intrusions.append(
                            {
                                'uid': member_uid,
                                'change': 'route_member',
                                'kind': ref_obj.kind if ref_obj else 'route',
                                'name': _object_label(ref_obj, member_uid),
                                'lpp': _object_lpp(ref_obj),
                                'bounds_um': _bounds_um(b, scale),
                                'old_bounds_um': _bounds_um(b, scale),
                                'region_id': reg_id,
                                'region_name': region.name,
                                'region_bounds_um': _bounds_um(region.bbox, scale),
                            }
                        )

    if intrusions:
        raise _format_intrusion_error(intrusions)

    return {
        'added': added,
        'modified': modified,
        'deleted': deleted,
        'old_bounds': old_bounds_map,
        'new_bounds': new_bounds_map,
    }


def guard_specs_for_workspace(w: Workspace) -> dict[str, dict[str, Any]]:
    """Collect guard generation specs from guard_previews with operations fallback."""
    specs: dict[str, dict[str, Any]] = {}
    for uid, preview in w.guard_previews.items():
        if isinstance(preview, dict) and preview.get('guard') is not None:
            specs[uid] = preview['guard']
    for op in w.operations:
        if op.action == 'guard' and op.guard and op.uid not in specs:
            specs[op.uid] = op.guard
    return specs


def guard_final_placement(uid: str, final_obj: Object, preview: dict[str, Any] | None) -> Placement | None:
    """Return final Placement if the created guard moved from its preview anchor."""
    raw = (preview or {}).get('objects', {}).get(uid)
    if raw is None:
        return final_obj.placement
    initial = Object.model_validate(raw)
    return final_obj.placement if final_obj.placement != initial.placement else None


def _local_snapshot_diffs(
    w: Workspace,
) -> tuple[dict[str, Object], set[str], set[str], set[str]]:
    base_objs = w.baseline.objects
    local_objs = projected(w, text_owners=w.baseline.text_owners)
    ignored_text = set(w.baseline.text_owners.keys())

    created_uids = {uid for uid in local_objs.keys() - base_objs.keys() if uid not in ignored_text}
    deleted_uids = {uid for uid in base_objs.keys() - local_objs.keys() if uid not in ignored_text}
    modified_uids = {
        uid
        for uid in (base_objs.keys() & local_objs.keys()) - ignored_text
        if canonical_object(base_objs[uid]) != canonical_object(local_objs[uid])
    }
    return local_objs, created_uids, modified_uids, deleted_uids


def local_snapshot_diff_scopes(
    w: Workspace, profile
) -> tuple[dict[str, Object], set[str], set[str], set[str], dict[str, str | None]]:
    """Compute net local differences purely between baseline and projected final state."""
    base_objs = w.baseline.objects
    local_objs, created_uids, modified_uids, deleted_uids = _local_snapshot_diffs(w)

    owners = owner_map(w)
    regions = w.local_data.regions
    base_only = w.model_copy(update={'operations': (), 'guard_previews': {}})
    scope_by_uid: dict[str, str | None] = {}

    for uid in created_uids | modified_uids:
        if uid in owners:
            scope_by_uid[uid] = owners[uid]
        elif regions:
            bounds = object_bounds(w, local_objs[uid], profile)
            scope_by_uid[uid] = enclosing_scope(regions, bounds) if bounds else None
        else:
            scope_by_uid[uid] = None

    group_scope_cache: dict[str, str | None] = {}
    for uid in deleted_uids:
        if uid in owners:
            scope_by_uid[uid] = owners[uid]
        elif regions:
            obj = base_objs[uid]
            if obj.group_uid and obj.mpp is None:
                gid = obj.group_uid
                if gid not in group_scope_cache:
                    members = [o for o in base_objs.values() if o.group_uid == gid]
                    member_scope = next((owners[m.uid] for m in members if m.uid in owners), None)
                    if member_scope is None:
                        bounds = _union_bounds(
                            object_bounds(base_only, m, profile) for m in members
                        )
                        member_scope = enclosing_scope(regions, bounds) if bounds else None
                    group_scope_cache[gid] = member_scope
                scope_by_uid[uid] = group_scope_cache[gid]
            else:
                bounds = object_bounds(base_only, obj, profile)
                scope_by_uid[uid] = enclosing_scope(regions, bounds) if bounds else None
        else:
            scope_by_uid[uid] = None

    return local_objs, created_uids, modified_uids, deleted_uids, scope_by_uid


def _transformed_text_owners(
    base_snapshot: Snapshot, desired_objects: dict[str, Object]
) -> dict[str, Object]:
    from .geometry import ORIENT, box, compose, transform

    updated = dict(desired_objects)
    for text_uid, parent_uid in base_snapshot.text_owners.items():
        old_parent = base_snapshot.objects.get(parent_uid)
        new_parent = updated.get(parent_uid)
        text_obj = updated.get(text_uid)
        if (
            old_parent is None
            or new_parent is None
            or text_obj is None
            or old_parent.placement is None
            or new_parent.placement is None
            or old_parent.placement == new_parent.placement
            or not text_obj.bbox
        ):
            continue
        a, b, c, d = ORIENT[old_parent.placement.orientation]
        x, y = old_parent.placement.origin
        inverse = (a, c, b, d, -a * x - c * y, -b * x - d * y)
        delta = compose(
            transform(new_parent.placement.origin, new_parent.placement.orientation),
            inverse,
        )
        updated[text_uid] = text_obj.model_copy(update={'bbox': box(text_obj.bbox, delta)})
    return updated


def overlay_local_snapshot(
    w: Workspace,
    base_snapshot: Snapshot,
    profile=None,
    *,
    active_scopes: set[str | None] | None = None,
    only_uids: set[str] | None = None,
) -> dict[str, Object]:
    """Overlay CoLayout's final reviewed region diffs directly onto base_snapshot."""
    if active_scopes is not None:
        local_objs, created_uids, modified_uids, deleted_uids, scope_by_uid = (
            local_snapshot_diff_scopes(w, profile)
        )
    else:
        local_objs, created_uids, modified_uids, deleted_uids = _local_snapshot_diffs(w)
        scope_by_uid = {}

    def selected(uid: str) -> bool:
        if active_scopes is not None and scope_by_uid[uid] not in active_scopes:
            return False
        if only_uids is not None and uid not in only_uids:
            return False
        return True

    desired = dict(base_snapshot.objects)
    for uid in deleted_uids:
        if selected(uid):
            desired.pop(uid, None)

    for uid in modified_uids:
        if not selected(uid):
            continue
        old_obj = desired.get(uid)
        local_obj = local_objs[uid]
        if old_obj is None:
            desired[uid] = local_obj
            continue
        if old_obj.kind == 'inst':
            base_old = w.baseline.objects[uid]
            if (
                local_obj.placement is None
                or canonical_object(base_old.model_copy(update={'placement': local_obj.placement}))
                != canonical_object(local_obj)
            ):
                raise LiteError('SOURCE_CONFLICT', f'对象 {uid} 本地更改超出位姿变换范围。', uid=uid)
            desired[uid] = old_obj.model_copy(update={'placement': local_obj.placement})
        elif old_obj.mpp is not None:
            base_old = w.baseline.objects[uid]
            if (
                local_obj.placement is None
                or canonical_object(placed(base_old, local_obj.placement))
                != canonical_object(local_obj)
            ):
                raise LiteError('SOURCE_CONFLICT', f'MPP 对象 {uid} 本地更改超出位姿变换范围。', uid=uid)
            desired[uid] = placed(old_obj, local_obj.placement)
        elif local_obj.shape is not None:
            desired[uid] = old_obj.model_copy(
                update={
                    'kind': local_obj.shape.kind,
                    'shape': local_obj.shape,
                    'properties': intent_properties(old_obj.properties, local_obj.shape.net_intent),
                }
            )
        else:
            desired[uid] = local_obj

    for uid in created_uids:
        if not selected(uid):
            continue
        if uid in desired:
            raise LiteError('UID_EXISTS', uid)
        desired[uid] = local_objs[uid]

    return _transformed_text_owners(base_snapshot, desired)


def synthesize_operations_from_target(
    baseline: Snapshot,
    target_objects: dict[str, Object],
    *,
    guard_specs: dict[str, dict[str, Any]] | None = None,
    guard_previews: dict[str, Any] | None = None,
) -> tuple[tuple[Operation, ...], dict[str, Any]]:
    """Synthesize normalized Operations purely from (baseline, target_objects) without edit history."""
    base_objs = baseline.objects
    ignored_text = set(baseline.text_owners.keys())
    specs = guard_specs or {}
    previews = guard_previews or {}
    ops: list[Operation] = []
    used_previews: dict[str, Any] = {}

    deleted_groups: set[str] = set()
    for uid in sorted(set(base_objs.keys()) - set(target_objects.keys()) - ignored_text):
        base_obj = base_objs[uid]
        if base_obj.group_uid and base_obj.mpp is None:
            gid = base_obj.group_uid
            if any(o.group_uid == gid for o in target_objects.values()):
                raise LiteError('PROTECTED_OBJECT', uid)
            if gid not in deleted_groups:
                deleted_groups.add(gid)
                ops.append(Operation(action='delete_guard', uid=gid))
        else:
            ops.append(Operation(action='delete', uid=uid, before=digest(base_obj.semantic())))

    for uid in sorted((set(base_objs.keys()) & set(target_objects.keys())) - ignored_text):
        base_obj = base_objs[uid]
        target_obj = target_objects[uid]
        if canonical_object(base_obj) == canonical_object(target_obj):
            continue
        before_digest = digest(base_obj.semantic())
        if target_obj.kind == 'inst' or target_obj.mpp is not None:
            ops.append(
                Operation(
                    action='transform',
                    uid=uid,
                    before=before_digest,
                    placement=target_obj.placement,
                )
            )
        elif target_obj.shape is not None:
            ops.append(
                Operation(
                    action='update',
                    uid=uid,
                    before=before_digest,
                    shape=target_obj.shape,
                )
            )
        else:
            raise LiteError('INSTANCE_OR_OPAQUE_READ_ONLY', uid)

    for uid in sorted(set(target_objects.keys()) - set(base_objs.keys()) - ignored_text):
        target_obj = target_objects[uid]
        if target_obj.mpp is not None:
            spec = specs.get(uid)
            preview = previews.get(uid)
            if not spec or not preview or preview.get('command_digest') != digest(spec):
                raise LiteError('GUARD_PREVIEW_REQUIRED')
            pl = guard_final_placement(uid, target_obj, preview)
            ops.append(Operation(action='guard', uid=uid, guard=spec, placement=pl))
            used_previews[uid] = preview
        elif target_obj.shape is not None:
            ops.append(Operation(action='create', uid=uid, shape=target_obj.shape))
        else:
            raise LiteError('INSTANCE_OR_OPAQUE_READ_ONLY', uid)

    return tuple(ops), used_previews


def build_snapshot_overwrite_commands(
    target_objects: dict[str, Object],
    current: Snapshot,
    *,
    profile=None,
    guard_specs: dict[str, dict[str, Any]] | None = None,
    guard_previews: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build history-free Virtuoso overwrite commands comparing target_objects against current."""
    from .codec import shape_payload

    curr_objs = current.objects
    ignored_text = set(current.text_owners.keys())
    specs = guard_specs or {}
    previews = guard_previews or {}
    commands: list[dict[str, Any]] = []

    deleted_groups: set[str] = set()
    for uid in sorted(set(curr_objs.keys()) - set(target_objects.keys()) - ignored_text):
        old_obj = curr_objs[uid]
        if old_obj.group_uid and old_obj.mpp is None:
            gid = old_obj.group_uid
            if any(o.group_uid == gid for o in target_objects.values()):
                raise LiteError('PROTECTED_OBJECT', uid)
            if gid not in deleted_groups:
                deleted_groups.add(gid)
                commands.append({'action': 'delete_guard', 'uid': gid})
        else:
            commands.append({'action': 'delete', 'uid': uid})

    for uid in sorted((set(curr_objs.keys()) & set(target_objects.keys())) - ignored_text):
        old_obj = curr_objs[uid]
        new_obj = target_objects[uid]
        if canonical_object(old_obj) == canonical_object(new_obj):
            continue
        if new_obj.kind == 'inst' or old_obj.mpp is not None:
            if new_obj.placement is None:
                raise LiteError('EXPECTED_INSTANCE_OR_MPP', uid)
            expected = (
                old_obj.model_copy(update={'placement': new_obj.placement})
                if new_obj.kind == 'inst'
                else placed(old_obj, new_obj.placement)
            )
            if canonical_object(expected) != canonical_object(new_obj):
                raise LiteError(
                    'SOURCE_CONFLICT', f'对象 {uid} 状态变化不能仅通过位姿更新完成。', uid=uid
                )
            commands.append(
                {
                    'action': 'transform',
                    'uid': uid,
                    'placement': {
                        'origin': list(map(str, new_obj.placement.origin)),
                        'orientation': new_obj.placement.orientation,
                    },
                }
            )

    for uid in sorted((set(curr_objs.keys()) & set(target_objects.keys())) - ignored_text):
        old_obj = curr_objs[uid]
        new_obj = target_objects[uid]
        if canonical_object(old_obj) == canonical_object(new_obj):
            continue
        if new_obj.kind == 'inst' or old_obj.mpp is not None:
            continue
        if new_obj.shape is None:
            raise LiteError('INSTANCE_OR_OPAQUE_READ_ONLY', uid)
        shape = new_obj.shape
        if profile is not None and shape.kind == 'stdVia':
            shape = shape.model_copy(update={'via_parameters': profile.effective_via(shape)})
        commands.append({'action': 'update', 'uid': uid, 'shape': shape_payload(shape)})

    for uid in sorted(set(target_objects.keys()) - set(curr_objs.keys()) - ignored_text):
        new_obj = target_objects[uid]
        if new_obj.mpp is not None:
            spec = specs.get(uid)
            preview = previews.get(uid)
            if not spec or not preview or preview.get('command_digest') != digest(spec):
                raise LiteError('GUARD_PREVIEW_REQUIRED')
            cmd: dict[str, Any] = {'action': 'guard', 'uid': uid, 'guard': spec}
            pl = guard_final_placement(uid, new_obj, preview)
            if pl is not None:
                cmd['placement'] = {
                    'origin': list(map(str, pl.origin)),
                    'orientation': pl.orientation,
                }
            commands.append(cmd)
        elif new_obj.shape is not None:
            shape = new_obj.shape
            if profile is not None and shape.kind == 'stdVia':
                shape = shape.model_copy(update={'via_parameters': profile.effective_via(shape)})
            commands.append({'action': 'create', 'uid': uid, 'shape': shape_payload(shape)})
        else:
            raise LiteError('INSTANCE_OR_OPAQUE_READ_ONLY', uid)

    return commands


def remaining_local_operations(
    full_w: Workspace,
    tx_w: Workspace,
    target_baseline: Snapshot,
    profile,
) -> tuple[tuple[Operation, ...], dict[str, Any]]:
    """Synthesize surviving deferred local operations onto target_baseline without history replay."""
    _, full_created, full_modified, full_deleted = _local_snapshot_diffs(full_w)
    _, tx_created, tx_modified, tx_deleted = _local_snapshot_diffs(tx_w)
    unsynced_uids = (full_created | full_modified | full_deleted) - (
        tx_created | tx_modified | tx_deleted
    )
    if not unsynced_uids:
        return (), {}
    desired = overlay_local_snapshot(
        full_w, target_baseline, profile, only_uids=unsynced_uids
    )
    return synthesize_operations_from_target(
        target_baseline,
        desired,
        guard_specs=guard_specs_for_workspace(full_w),
        guard_previews=full_w.guard_previews,
    )


def _uid_diff_bounds(
    w: Workspace, base_only: Workspace, local_objs: dict[str, Object], uid: str, profile
):
    boxes = []
    if uid in w.baseline.objects:
        b = object_bounds(base_only, w.baseline.objects[uid], profile)
        if b is not None:
            boxes.append(b)
    if uid in local_objs:
        b = object_bounds(w, local_objs[uid], profile)
        if b is not None:
            boxes.append(b)
    return boxes


def _without_deferred_creations(local_data, deferred_created_uids: set[str]):
    """Strip UIDs created only in deferred scopes from region membership during partial tx validation."""
    if not deferred_created_uids:
        return local_data
    regions = {
        rid: region.model_copy(
            update={'object_uids': tuple(u for u in region.object_uids if u not in deferred_created_uids)}
        )
        if any(u in deferred_created_uids for u in region.object_uids)
        else region
        for rid, region in local_data.regions.items()
    }
    return local_data.model_copy(update={'regions': regions})


def rebase_workspace_onto_virtuoso(
    w: Workspace,
    current: Snapshot,
    profile,
    *,
    circuit=None,
    connectivity=None,
    scope_region_ids: set[str] | None = None,
    exclude_region_ids: set[str] | None = None,
) -> tuple[Workspace, Workspace, tuple[Operation, ...], dict]:
    """Merge Virtuoso's outside-region changes beneath CoLayout's final reviewed region snapshots.

    Returns `(full_rebased_w, tx_w, retained_ops, delta)`:
    - `full_rebased_w`: merged baseline + synthesized CoLayout final-snapshot operations.
    - `tx_w`: merged baseline + synthesized operations selected for immediate Virtuoso overwrite.
    - `retained_ops`: operations deferred locally (when scoping out busy/other regions).
    - `delta`: Virtuoso added/modified/deleted UID summary.
    """
    delta = analyze_virtuoso_delta(w, current, profile)
    changed_uids = set(delta['added']) | set(delta['modified']) | set(delta['deleted'])
    v_boxes = [
        b
        for uid in changed_uids
        for b in (delta['old_bounds'].get(uid), delta['new_bounds'].get(uid))
        if b is not None
    ]
    missing_v_bounds = any(
        delta['old_bounds'].get(uid) is None and delta['new_bounds'].get(uid) is None
        for uid in changed_uids
    )

    if scope_region_ids is not None:
        unknown = [rid for rid in scope_region_ids if rid not in w.local_data.regions]
        if unknown:
            raise LiteError('REGION_MISSING', unknown[0])
        allowed_scopes = set()
        for rid in scope_region_ids:
            allowed_scopes.update(descendants(w, rid))
    else:
        allowed_scopes = None

    excluded_scopes = set()
    if exclude_region_ids:
        for rid in exclude_region_ids:
            if rid in w.local_data.regions:
                excluded_scopes.update(descendants(w, rid))

    base_only = w.model_copy(update={'operations': (), 'guard_previews': {}})
    local_objs, created_uids, modified_uids, deleted_uids, scope_by_uid = (
        local_snapshot_diff_scopes(w, profile)
    )
    local_changed_uids = created_uids | modified_uids | deleted_uids

    for uid in sorted(local_changed_uids):
        uid_scope = scope_by_uid[uid]
        if uid in changed_uids:
            require_unchanged(w, current)
        if uid_scope is None and changed_uids:
            uid_boxes = _uid_diff_bounds(w, base_only, local_objs, uid, profile)
            if not uid_boxes or missing_v_bounds or any(overlaps(ub, vb) for ub in uid_boxes for vb in v_boxes):
                require_unchanged(w, current)

    all_scopes = set(scope_by_uid.values())
    selected_scopes = {
        s
        for s in all_scopes
        if (allowed_scopes is None or s in allowed_scopes) and (s not in excluded_scopes)
    }

    merged_baseline = retain_display(w.baseline, current)
    merged_circuit = circuit if circuit is not None else w.circuit
    merged_connectivity = (
        connectivity
        if connectivity is not None
        else rebind_layout(w.layout_connectivity, merged_baseline)
    )

    guard_specs = guard_specs_for_workspace(w)
    full_desired = overlay_local_snapshot(w, merged_baseline, profile)
    tx_desired = overlay_local_snapshot(
        w, merged_baseline, profile, active_scopes=selected_scopes
    )
    all_ops, full_previews = synthesize_operations_from_target(
        merged_baseline,
        full_desired,
        guard_specs=guard_specs,
        guard_previews=w.guard_previews,
    )
    commit_ops, commit_previews = synthesize_operations_from_target(
        merged_baseline,
        tx_desired,
        guard_specs=guard_specs,
        guard_previews=w.guard_previews,
    )
    deferred_uids = {
        uid for uid in local_changed_uids if scope_by_uid[uid] not in selected_scopes
    }
    deferred_groups = {
        w.baseline.objects[uid].group_uid
        for uid in deferred_uids & deleted_uids
        if w.baseline.objects[uid].group_uid and w.baseline.objects[uid].mpp is None
    }
    retained_ops = tuple(
        op
        for op in all_ops
        if op.uid in deferred_uids or (op.action == 'delete_guard' and op.uid in deferred_groups)
    )

    full_rebased_w = w.model_copy(
        update={
            'baseline': merged_baseline,
            'circuit': merged_circuit,
            'layout_connectivity': merged_connectivity,
            'operations': all_ops,
            'guard_previews': full_previews,
            'revision': w.revision + 1 if not same_snapshot(w.baseline, current) else w.revision,
            'state': 'editing' if all_ops else 'clean',
        }
    )
    require_references(full_rebased_w.local_data, projected(full_rebased_w))
    if (
        (
            full_rebased_w.local_data.pins
            or full_rebased_w.local_data.regions
            or full_rebased_w.local_data.keepouts
            or full_rebased_w.local_data.markups
            or full_rebased_w.local_data.native_routing_windows
        )
        and merged_baseline.technology_fingerprint != w.baseline.technology_fingerprint
    ):
        raise LiteError('LOCAL_PIN_TECHNOLOGY_CHANGED', '工艺/单位变化，不能自动解释旧本地 PIN 坐标。')
    if full_rebased_w.operations:
        validate_pending(full_rebased_w, profile)
    else:
        validate_workspace(full_rebased_w, profile)

    deferred_created_uids = {
        uid for uid in created_uids if scope_by_uid[uid] not in selected_scopes
    }
    tx_local_data = _without_deferred_creations(w.local_data, deferred_created_uids)
    tx_w = full_rebased_w.model_copy(
        update={
            'local_data': tx_local_data,
            'operations': commit_ops,
            'guard_previews': commit_previews,
            'state': 'editing' if commit_ops else 'clean',
        }
    )
    if tx_w.operations:
        require_references(tx_w.local_data, projected(tx_w))
    validate_pending(tx_w, profile) if tx_w.operations else validate_workspace(tx_w, profile)

    return full_rebased_w, tx_w, retained_ops, delta
