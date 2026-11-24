"""Rebind draft preconditions only after proving a private clone is equivalent.

Cadence may enumerate named MPP subparts in a different order after dbCopyCellView.
Names, multiplicity, every definition field and all other geometry stay strict.
Original operation hashes and transaction payloads are never rewritten in place.
"""

from .cooperative_sync import (
    guard_specs_for_workspace,
    overlay_local_snapshot,
    synthesize_operations_from_target,
)
from .hierarchy import same_dependencies
from .model import LiteError
from .sync_checks import canonical_object


def clone_object(obj):
    value = canonical_object(obj)
    if obj.mpp:
        parts = value['mpp']['parts']
        names = [part.get('name') for part in parts if isinstance(part, dict)]
        # Only named, unique subparts are an unordered native collection.
        # Unknown formats remain strictly ordered, including duplicates.
        if (
            len(names) == len(parts)
            and all(isinstance(n, str) and n for n in names)
            and len(set(names)) == len(names)
        ):
            value['mpp']['parts'] = sorted(parts, key=lambda p: p['name'])
    return value


def bind(source, private):
    old = source.baseline
    new = private.baseline
    a = old.semantic()
    b = new.semantic()
    metadata = [key for key in a if key not in ('objects', 'dependencies') and a[key] != b[key]]
    changed = [
        uid
        for uid in old.objects.keys() | new.objects.keys()
        if uid not in old.objects
        or uid not in new.objects
        or clone_object(old.objects[uid]) != clone_object(new.objects[uid])
    ]
    if changed or metadata or not same_dependencies(old, new):
        raise LiteError(
            'PRIVATE_COPY_SOURCE_MISMATCH',
            '私有副本与源基准不一致，未应用草稿；请核对外部变化。',
            objects=changed[:20],
            metadata=metadata,
            dependencies_match=same_dependencies(old, new),
        )
    desired = overlay_local_snapshot(source, new, None)
    operations, previews = synthesize_operations_from_target(
        new,
        desired,
        guard_specs=guard_specs_for_workspace(source),
        guard_previews=source.guard_previews,
    )
    return private.model_copy(
        update={
            'operations': operations,
            'guard_previews': previews,
            'local_data': source.local_data,
        }
    )
