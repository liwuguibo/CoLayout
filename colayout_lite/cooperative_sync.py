"""Domain rules for decoupled Virtuoso <-> CoLayout cooperative synchronization.

Supports three-way spatial merging when Human edits in Virtuoso and AI/Human edits
in CoLayout occupy disjoint regions, rejects Virtuoso edits overlapping any
OperationRegion with actionable object/coordinate diagnostics, and builds
non-fabricating native Virtuoso region warning guide specifications.
"""

from __future__ import annotations

from .circuit.query import rebind_layout
from .editor import projected
from .groups import require_groups
from .hierarchy import retain_display, same_dependencies
from .local_models import require_references
from .model import LiteError, Operation, Snapshot, Workspace, digest
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


def operation_scope_id(w: Workspace, op: Operation, profile) -> str | None:
    """Resolve the deepest enclosing OperationRegion owning an uncommitted Operation."""
    owners = owner_map(w)
    if op.uid in owners:
        return owners[op.uid]
    regions = w.local_data.regions
    if not regions:
        return None
    base_only = w.model_copy(update={'operations': (), 'guard_previews': {}})
    if op.action == 'delete' and op.uid in w.baseline.objects:
        bounds = object_bounds(base_only, w.baseline.objects[op.uid], profile)
        return enclosing_scope(regions, bounds) if bounds else None
    if op.action == 'delete_guard':
        guard_objs = [o for o in w.baseline.objects.values() if o.group_uid == op.uid]
        for obj in guard_objs:
            if obj.uid in owners:
                return owners[obj.uid]
        bounds = _union_bounds(object_bounds(base_only, o, profile) for o in guard_objs)
        return enclosing_scope(regions, bounds) if bounds else None
    proj = projected(w)
    if op.action == 'guard':
        guard_objs = [o for o in proj.values() if o.uid == op.uid or o.group_uid == op.uid]
        for obj in guard_objs:
            if obj.uid in owners:
                return owners[obj.uid]
        bounds = _union_bounds(object_bounds(w, o, profile) for o in guard_objs)
        return enclosing_scope(regions, bounds) if bounds else None
    obj = proj.get(op.uid)
    if obj is not None:
        bounds = object_bounds(w, obj, profile)
        if bounds is not None:
            return enclosing_scope(regions, bounds)
    return None


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


def _op_bounds(w: Workspace, base_only: Workspace, proj: dict, op: Operation, profile):
    boxes = []
    if op.action == 'delete_guard':
        for obj in w.baseline.objects.values():
            if obj.group_uid == op.uid:
                boxes.append(object_bounds(base_only, obj, profile))
        return [b for b in boxes if b is not None]
    if op.action in ('update', 'delete', 'transform') and op.uid in w.baseline.objects:
        b = object_bounds(base_only, w.baseline.objects[op.uid], profile)
        if b is not None:
            boxes.append(b)
    if op.action == 'guard':
        for obj in proj.values():
            if obj.uid == op.uid or obj.group_uid == op.uid:
                b = object_bounds(w, obj, profile)
                if b is not None:
                    boxes.append(b)
        return boxes
    if op.uid in proj and op.action != 'delete':
        b = object_bounds(w, proj[op.uid], profile)
        if b is not None:
            boxes.append(b)
    return boxes


def rebind_operations(
    operations: tuple[Operation, ...] | list[Operation],
    source_baseline: Snapshot,
    target_snapshot: Snapshot,
) -> tuple[Operation, ...]:
    """Verify precondition equivalence and update op.before hashes onto target_snapshot."""
    rebound = []
    for op in operations:
        if op.action == 'create':
            if op.uid in target_snapshot.objects:
                raise LiteError('UID_EXISTS', op.uid)
            rebound.append(op)
        elif op.action == 'guard':
            if any(o.uid == op.uid or o.group_uid == op.uid for o in target_snapshot.objects.values()):
                raise LiteError('GROUP_EXISTS', op.uid)
            rebound.append(op)
        elif op.action == 'delete_guard':
            members = [o for o in target_snapshot.objects.values() if o.group_uid == op.uid]
            if not members:
                raise LiteError('GROUP_MISSING', op.uid)
            rebound.append(op)
        else:
            old_obj = source_baseline.objects.get(op.uid)
            new_obj = target_snapshot.objects.get(op.uid)
            if old_obj is None or new_obj is None:
                raise LiteError('OBJECT_CONFLICT', f'对象 {op.uid} 在最新 Virtuoso 版图中不存在。', uid=op.uid)
            if op.before != digest(old_obj.semantic()):
                raise LiteError('OBJECT_CONFLICT', f'本地草稿前置摘要与基准不一致：{op.uid}', uid=op.uid)
            if canonical_object(old_obj) != canonical_object(new_obj):
                raise LiteError('OBJECT_CONFLICT', f'Virtuoso 中的对象 {op.uid} 已被修改，无法合并。', uid=op.uid)
            rebound.append(op.model_copy(update={'before': digest(new_obj.semantic())}))
    return tuple(rebound)


def _without_retained_creations(local_data, retained_ops: tuple[Operation, ...], retained_guard_previews: dict):
    """Strip UIDs created only in deferred retained_ops from region membership during partial tx validation."""
    retained_uids = {op.uid for op in retained_ops if op.action == 'create'}
    for preview in retained_guard_previews.values():
        retained_uids.update(preview.get('objects', {}).keys())
    if not retained_uids:
        return local_data
    regions = {
        rid: region.model_copy(
            update={'object_uids': tuple(u for u in region.object_uids if u not in retained_uids)}
        )
        if any(u in retained_uids for u in region.object_uids)
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
    """Merge Virtuoso's outside-region changes beneath CoLayout's pending operations.

    Returns `(full_rebased_w, tx_w, retained_ops, delta)`:
    - `full_rebased_w`: merged baseline + ALL rebound CoLayout operations.
    - `tx_w`: merged baseline + only operations selected for immediate Virtuoso commit.
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
    proj = projected(w)

    selected_indices: list[int] = []
    deferred_indices: list[int] = []
    for idx, op in enumerate(w.operations):
        op_scope = operation_scope_id(w, op, profile)
        if op.uid in changed_uids:
            require_unchanged(w, current)
        if op_scope is None and changed_uids:
            op_boxes = _op_bounds(w, base_only, proj, op, profile)
            if not op_boxes or missing_v_bounds or any(overlaps(ob, vb) for ob in op_boxes for vb in v_boxes):
                require_unchanged(w, current)
        in_scope = (allowed_scopes is None or op_scope in allowed_scopes) and (
            op_scope not in excluded_scopes
        )
        if in_scope:
            selected_indices.append(idx)
        else:
            deferred_indices.append(idx)

    merged_baseline = retain_display(w.baseline, current)
    merged_circuit = circuit if circuit is not None else w.circuit
    merged_connectivity = (
        connectivity
        if connectivity is not None
        else rebind_layout(w.layout_connectivity, merged_baseline)
    )

    if not same_snapshot(w.baseline, current):
        # Preserve original operation ordering after rebinding to Virtuoso's baseline.
        all_rebound = rebind_operations(w.operations, w.baseline, merged_baseline)
    else:
        all_rebound = tuple(w.operations)

    commit_ops = tuple(all_rebound[i] for i in selected_indices)
    retained_ops = tuple(all_rebound[i] for i in deferred_indices)

    if any(u not in merged_baseline.objects for r in w.local_data.regions.values() for u in r.object_uids):
        # Region intrusions already raised VIRTUOSO_EDIT_IN_REGION above; only outside
        # orphaned references remain possible if a referenced local group/pin/route disappeared.
        pass

    full_rebased_w = w.model_copy(
        update={
            'baseline': merged_baseline,
            'circuit': merged_circuit,
            'layout_connectivity': merged_connectivity,
            'operations': all_rebound,
            'revision': w.revision + 1 if not same_snapshot(w.baseline, current) else w.revision,
            'state': 'editing' if all_rebound else 'clean',
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

    commit_uids = {op.uid for op in commit_ops}
    retained_previews = {k: v for k, v in w.guard_previews.items() if k not in commit_uids}
    commit_previews = {k: v for k, v in w.guard_previews.items() if k in commit_uids}
    tx_local_data = _without_retained_creations(w.local_data, retained_ops, retained_previews)
    tx_w = full_rebased_w.model_copy(
        update={
            'local_data': tx_local_data,
            'operations': commit_ops,
            'guard_previews': commit_previews,
            'state': 'editing' if commit_ops else 'clean',
        }
    )
    if tx_ops_need_refs := tx_w.operations:
        del tx_ops_need_refs
        require_references(tx_w.local_data, projected(tx_w))
    validate_pending(tx_w, profile) if tx_w.operations else validate_workspace(tx_w, profile)

    return full_rebased_w, tx_w, retained_ops, delta
