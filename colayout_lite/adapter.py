"""Paired source import and layout-only writes with receipt reconciliation."""

from __future__ import annotations
import time
from typing import TYPE_CHECKING
from uuid import uuid4
from .model import Workspace, Snapshot, Operation, LiteError, digest
from .codec import decode, shape_payload
from .paths import ROOT
from .transport import NativeTransport
from .native_lane import serialized_adapter
from .native_temporary import track as track_temporary,temporary_scope
from .hierarchy import same_dependencies, retain_display, merge_fragment
from .transactions import Receipt, TransactionJournal, checked_id, payload_hash

if TYPE_CHECKING:
    from .technology import TechnologyProfile


# Re-export these established helpers for existing callers and tests.
from .sync_checks import (
    normalized_vertices as normalized_vertices,
    canonical_object as canonical_object,
    require_unchanged,
    same_snapshot,
    verify,
)


class Adapter:
    def __init__(self, transport=None):
        self.native = transport or NativeTransport()
        self.state = self.native.state

    def read(self, library: str, cell: str) -> tuple[Snapshot, TechnologyProfile]:
        """Read layout/profile without preparing IDs or saving the design."""
        started = time.perf_counter()
        raw = self.native.read(library, cell)
        downloaded = time.perf_counter()
        return self._decode_layout(raw, started, downloaded)

    def _verify_saved_readback(self, workspace, actual, profile):
        """Persist only an exactly verified delayed ROD update, with a counter guard.

        Cadence can perform ROD maintenance after the transaction's first save.
        Never export that in-memory-only result or save an unverified native edit.
        This is not a replay: no edit operation is executed a second time.
        """
        for _ in range(3):
            verify(workspace,actual,profile=profile,require_saved=False)
            if not actual.dirty:return actual
            self.native.call('ltPersistVerified',actual.library,actual.cell,actual.counter)
            actual,_=self.read(actual.library,actual.cell)
        verify(workspace,actual,profile=profile)
        return actual

    def _decode_layout(self, raw, started=None, downloaded=None):
        started = started if started is not None else time.perf_counter()
        downloaded = downloaded if downloaded is not None else started
        snapshot, discovered = decode(raw)
        decoded = time.perf_counter()
        from .technology import TechnologyProfile

        folder = ROOT / self.native.config.get(
            "profiles_directory", "config/technologies"
        )
        matches = []
        preferred = self.native.config.get("preferred_profile")
        for path in folder.glob("*.json"):
            profile = TechnologyProfile.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if profile.native_fingerprint == discovered.native_fingerprint:
                if profile.native_technology_name and profile.native_technology_name!=discovered.native_technology_name:
                    continue
                if (
                    profile.dbu_per_micron != discovered.dbu_per_micron
                    or profile.grid % discovered.grid
                ):
                    raise LiteError("PROFILE_UNIT_MISMATCH", str(path))
                if (
                    profile.vias != discovered.vias
                    or profile.native_layers != discovered.native_layers
                ):
                    raise LiteError("PROFILE_NATIVE_DATA_CHANGED", str(path))
                # Factories are explicit profile configuration. Their exact
                # native definition is checked by ltGuard at generation time.
                matches.append(profile)
        if preferred:
            selected = [p for p in matches if p.id == preferred]
            if not selected:
                raise LiteError(
                    "CONNECTED_PDK_MISMATCH",
                    "连接中的实际工艺与选定配置不一致；选择配置文件不会切换服务器 PDK。",
                )
            matches = selected
        if len(matches) > 1:
            raise LiteError("AMBIGUOUS_TECHNOLOGY_PROFILE")
        profile = matches[0] if matches else discovered
        from .permissions import apply_profile_permissions

        snapshot = apply_profile_permissions(snapshot, profile)
        self.last_read_timings = {
            **getattr(self.native, "last_read_timings", {}),
            "decode_s": decoded - downloaded,
            "profile_s": time.perf_counter() - decoded,
            "total_s": time.perf_counter() - started,
        }
        return snapshot, profile

    def read_pair(self, library: str, cell: str, *, schematic=None):
        """Acquire and validate both views before a caller may install a workspace.

        The low-level read() remains available for transaction recovery/audits;
        GUI import and refresh always use this strict paired path.
        """
        from .circuit.codec import decode_context
        started=time.perf_counter()
        raw = self.native.read_pair(library, cell, schematic)
        snapshot, profile = self._decode_layout(raw[1],started,time.perf_counter())
        decoded=time.perf_counter()
        try:
            circuit, connectivity = decode_context(raw[2], snapshot)
        except LiteError as exc:
            from .transactions import write_json_atomic
            write_json_atomic(self.state/'pairing-reports'/f'{uuid4().hex}.json', {
                'status': 'rejected', 'library': library, 'cell': cell,
                'code': exc.code, 'details': exc.details,
                'export': getattr(self.native, 'last_paired_export', None)})
            raise
        self.last_read_timings.update(context_s=time.perf_counter()-decoded,total_s=time.perf_counter()-started)
        return snapshot, profile, circuit, connectivity

    def create_workspace(
        self, library: str, source_cell: str, *, label="TARGET", schematic=None
    ) -> tuple[Workspace, TechnologyProfile]:
        """Explicitly create a private layout copy for tests/preview/recovery."""
        cell = "COLAYOUT_MVP_LITE_" + label + "_" + uuid4().hex[:10]
        # Resolve the source of a source-layout that may itself be a private copy.
        if schematic is None:
            _, _, original, _ = self.read_pair(library, source_cell)
            schematic = (original.source.library, original.source.cell, original.source.view)
        self.native.call("lcClonePaired", library, source_cell, cell, uuid4().hex)
        track_temporary(self,library,cell,source_cell)
        snapshot, profile, circuit, connectivity = self.read_pair(library, cell, schematic=schematic)
        workspace = Workspace(
            baseline=snapshot,
            circuit=circuit,
            layout_connectivity=connectivity,
            profile_id=profile.id,
            connection_config=str(self.native.config_path),
        )
        self.save(workspace)
        return workspace, profile

    def open_workspace(
        self, library: str, cell: str, *, schematic=None
    ) -> tuple[Workspace, TechnologyProfile]:
        """Bind the selected existing Layout; never clone or redirect its target."""
        preflight=getattr(self.native,'preflight_layout',None)
        if preflight:preflight(library,cell)
        self.native.call("ltPrepare", library, cell, uuid4().hex)
        snapshot, profile, circuit, connectivity = self.read_pair(library, cell, schematic=schematic)
        workspace = Workspace(
            baseline=snapshot,
            circuit=circuit,
            layout_connectivity=connectivity,
            profile_id=profile.id,
            connection_config=str(self.native.config_path),
        )
        self.save(workspace)
        return workspace, profile

    def create_validation_workspace(self,library,source_cell,*,label='PROCESS_VALIDATION'):
        """Configuration-only geometry copy; never substitutes normal paired import."""
        import re
        if not re.fullmatch(r'[A-Z0-9_]{1,40}',label):raise LiteError('INVALID_VALIDATION_LABEL')
        original,_=self.read(library,source_cell)
        if original.dirty:raise LiteError('SAVE_SAMPLE_FIRST')
        cell='COLAYOUT_MVP_LITE_'+label+'_'+uuid4().hex[:10]
        self.native.call('ltClone',library,source_cell,cell,uuid4().hex)
        track_temporary(self,library,cell,source_cell)
        snapshot,profile=self.read(library,cell)
        workspace=Workspace(baseline=snapshot,profile_id=profile.id,
            connection_config=str(self.native.config_path),validation_only=True)
        self.save(workspace);return workspace,profile

    def save(self, w: Workspace) -> None:
        """Save local state only; this does not submit anything to Virtuoso."""
        w.save(self.state / "workspaces" / f"{w.id}.json")

    def load_workspace(self, workspace_id: str) -> Workspace:
        """Load the authoritative local state after a failed/unknown commit.

        commit() accepts immutable inputs. On failure, the input object still
        represents the old draft; use this persisted copy before reconcile().
        """
        checked_id(workspace_id, code="INVALID_WORKSPACE_ID")
        path = self.state / "workspaces" / f"{workspace_id}.json"
        workspace = Workspace.model_validate_json(path.read_text(encoding="utf-8"))
        if workspace.id != workspace_id:
            raise LiteError("WORKSPACE_ID_MISMATCH")
        return workspace

    def _require_settled(self, workspace):
        if workspace.state in ("indeterminate", "submitting"):
            raise LiteError("RECONCILE_BEFORE_RETRY")
        path = (
            self.state
            / "workspaces"
            / f"{checked_id(workspace.id, code='INVALID_WORKSPACE_ID')}.json"
        )
        if path.exists():
            saved = self.load_workspace(workspace.id)
            if saved.state in ("indeterminate", "submitting"):
                raise LiteError(
                    "RECONCILE_BEFORE_RETRY",
                    "请先 load_workspace(workspace_id) 并 reconcile；不能重发旧内存草稿。",
                    workspace_path=str(path),
                    request_id=saved.last_result.get("request_id"),
                )

    @staticmethod
    def _receipt(raw, workspace, request_id, expected_hash):
        return Receipt.parse(
            raw,
            request_id=request_id,
            expected_hash=expected_hash,
            library=workspace.baseline.library,
            cell=workspace.baseline.cell,
        )

    def expanded_display(self, w, uid):
        current, _ = self.read(w.baseline.library, w.baseline.cell)
        require_unchanged(w, current)
        if current.session != w.baseline.session:
            raise LiteError("DISPLAY_SESSION_CHANGED", "服务器会话已变化，请先拉取。")
        fragment = self.native.instance_display(
            current.library, current.cell, uid, current.counter, current.dependencies
        )
        if (
            fragment[0] != "colayout-instance-display-1"
            or fragment[1] != current.session
            or fragment[2] != uid
        ):
            raise LiteError("DISPLAY_RESPONSE_MISMATCH")
        if fragment[3] != w.baseline.objects[uid].attributes["master_key"]:
            raise LiteError("DISPLAY_MASTER_CHANGED")
        return merge_fragment(w.baseline.display, fragment)

    def fork_draft(self, w):
        """Explicit recovery onto a new copy; never overwrite concurrent native edits."""
        if w.state in ("submitting", "indeterminate"):
            raise LiteError("RECOVERY_REQUIRED", "先核对未完成同步，再迁移草稿。")
        current, profile = self.read(w.baseline.library, w.baseline.cell)
        for key in (
            "dependencies",
            "terminals",
            "opaque_counts",
            "technology",
            "routes",
        ):
            if key == "dependencies" and same_dependencies(current, w.baseline):
                continue
            if current.semantic()[key] != w.baseline.semantic()[key]:
                raise LiteError(
                    "UNSAFE_DRAFT_MIGRATION",
                    "实例内部、端子或工艺发生变化，需要重新导入；原草稿保留。",
                )
        if any(op.action in ("guard", "delete_guard") for op in w.operations):
            raise LiteError(
                "GUARD_DRAFT_MIGRATION",
                "含未提交保护环的草稿请先保留备份并核对目标，不能自动迁移。",
            )
        from .editor import Editor

        ops = []
        for op in w.operations:
            if op.action != "create":
                obj = current.objects.get(op.uid)
                if obj is None:
                    if op.action == "delete":
                        continue
                    raise LiteError("DRAFT_TARGET_REMOVED", op.uid)
                op = op.model_copy(update={"before": digest(obj.semantic())})
            ops.append(op)
        check = Editor(Workspace(baseline=current, profile_id=profile.id), profile)
        check.apply(ops)
        w.save(self.state / "drafts" / f"{w.id}-before-fork-{uuid4().hex[:8]}.json")
        fork, p = self.create_workspace(current.library, current.cell, label="RECOVER")
        if fork.baseline.semantic() != current.semantic():
            raise LiteError("SOURCE_CHANGED_DURING_COPY")
        # Operations were validated against an identical native baseline above.
        # Preserve their local owners rather than replaying child edits at root.
        fork=fork.model_copy(update={'local_data':w.local_data,'operations':check.w.operations,
            'revision':fork.revision+1,'state':'editing' if check.w.operations else 'clean'})
        from .regions.constraints import validate_pending
        validate_pending(fork,p)
        result = fork.model_copy(
            update={
                "last_result": {
                    "status": "draft_migrated",
                    "original_cell": current.cell,
                }
            }
        )
        self.save(result)
        return result, p

    _PREPARE_RECOVERABLE_ERRORS = frozenset(
        {
            "DUPLICATE_ROUTE_ID",
            "DUPLICATE_ROUTE_MEMBERSHIP",
            "ROUTE_MEMBER_MISMATCH",
            "ROUTE_OWNER_MISMATCH",
            "DUPLICATE_FIGGROUP_ID",
        }
    )

    def _read_cooperative_source(self, library: str, cell: str):
        """Read Virtuoso snapshot, auto-preparing duplicate route/group IDs after GUI splits."""
        try:
            return self.read(library, cell)
        except LiteError as exc:
            if exc.code not in self._PREPARE_RECOVERABLE_ERRORS:
                raise
            from .cooperative_sync import translate_native_unsaved_error

            try:
                self.native.call("ltPrepare", library, cell, uuid4().hex)
            except LiteError as prep_exc:
                raise translate_native_unsaved_error(prep_exc) from prep_exc
            return self.read(library, cell)

    @serialized_adapter
    def sync_region_guides(self, w: Workspace) -> dict:
        """Draw or update non-fabricating OperationRegion warning guides in Virtuoso."""
        from .cooperative_sync import build_region_guides

        if not w.baseline.library or not w.baseline.cell or w.validation_only:
            return {"synced": False, "reason": "unconfigured"}
        if w.baseline.cell.startswith("COLAYOUT_MVP_LITE_"):
            return {"synced": False, "reason": "temporary"}
        guides = build_region_guides(w)
        reply = self.native.call(
            "ltSyncRegionGuides", w.baseline.library, w.baseline.cell, guides
        )
        return {"synced": True, "count": len(guides), "reply": reply}

    def _read_with_context(self, w: Workspace):
        if w.circuit is not None and hasattr(self.native, "read_pair"):
            source = w.circuit.source
            return self.read_pair(
                w.baseline.library,
                w.baseline.cell,
                schematic=(source.library, source.cell, source.view),
            )
        current, profile = self.read(w.baseline.library, w.baseline.cell)
        return current, profile, w.circuit, self._rebind_connectivity(w, current)

    @serialized_adapter
    def cooperative_pull(self, w: Workspace):
        """Pull outside-region Virtuoso changes without discarding pending region drafts."""
        from .cooperative_sync import (
            analyze_virtuoso_delta,
            needs_native_prepare,
            rebase_workspace_onto_virtuoso,
            translate_native_unsaved_error,
        )

        self._require_settled(w)
        current, profile = self._read_cooperative_source(w.baseline.library, w.baseline.cell)
        analyze_virtuoso_delta(w, current, profile)
        if needs_native_prepare(current):
            try:
                self.native.call(
                    "ltPrepare", w.baseline.library, w.baseline.cell, uuid4().hex
                )
            except LiteError as exc:
                raise translate_native_unsaved_error(exc) from exc
            current, profile, circuit, connectivity = self._read_with_context(w)
        elif w.circuit is not None and hasattr(self.native, "read_pair") and not same_snapshot(w.baseline, current):
            current, profile, circuit, connectivity = self._read_with_context(w)
        else:
            circuit = w.circuit
            connectivity = self._rebind_connectivity(w, current)

        result, _, _, delta = rebase_workspace_onto_virtuoso(
            w, current, profile, circuit=circuit, connectivity=connectivity
        )
        guide_warning = None
        if getattr(self.native, "config_path", None) is not None:
            try:
                self.sync_region_guides(result)
            except Exception as exc:
                guide_warning = str(exc)
        last_result = {
            "status": "cooperative_refreshed",
            "virtuoso_delta": {
                "added": len(delta["added"]),
                "modified": len(delta["modified"]),
                "deleted": len(delta["deleted"]),
            },
            "pending_operations": len(result.operations),
        }
        if guide_warning:
            last_result["guide_sync_warning"] = guide_warning
        result = result.model_copy(update={"last_result": last_result})
        self.save(result)
        return result, profile

    def refresh(self, w):
        if w.operations:
            raise LiteError("LOCAL_DRAFT_EXISTS")
        self.native.call("ltPrepare", w.baseline.library, w.baseline.cell, uuid4().hex)
        source = w.circuit.source if w.circuit else None
        schematic = (source.library, source.cell, source.view) if source else None
        snapshot, profile, circuit, connectivity = self.read_pair(w.baseline.library, w.baseline.cell, schematic=schematic)
        snapshot = retain_display(w.baseline, snapshot)
        local_data = w.local_data
        if any(uid not in snapshot.objects for r in local_data.regions.values() for uid in r.object_uids):
            pruned_regions = {
                rid: r.model_copy(
                    update={
                        "object_uids": tuple(uid for uid in r.object_uids if uid in snapshot.objects),
                        "revision": r.revision + 1,
                    }
                )
                if any(uid not in snapshot.objects for uid in r.object_uids)
                else r
                for rid, r in local_data.regions.items()
            }
            local_data = local_data.model_copy(update={"regions": pruned_regions})
        from .local_models import require_references
        require_references(local_data,snapshot.objects)
        if (local_data.pins or local_data.regions or local_data.keepouts or local_data.markups or local_data.native_routing_windows) and snapshot.technology_fingerprint!=w.baseline.technology_fingerprint:
            raise LiteError('LOCAL_PIN_TECHNOLOGY_CHANGED','工艺/单位变化，不能自动解释旧本地 PIN 坐标。')
        result = Workspace(
            baseline=snapshot,
            circuit=circuit,
            layout_connectivity=connectivity,
            local_data=local_data,
            profile_id=profile.id,
            connection_config=w.connection_config,
            instance_display={
                k: v for k, v in w.instance_display.items() if k in snapshot.objects
            },
        )
        from .regions.import_ownership import assign_new_objects
        result, imported_memberships = assign_new_objects(w, result, profile)
        if imported_memberships:
            result = result.model_copy(update={'last_result': {'status': 'refreshed',
                'imported_region_memberships': imported_memberships}})
        from .regions.constraints import validate_workspace
        validate_workspace(result,profile)
        self.save(result)
        return result, profile

    def upgrade_metadata(self, w):
        """Read-only migration; explicit prepare/import establishes persistent route IDs."""
        from .metadata_upgrade import rebase_metadata

        current, profile = self.read(w.baseline.library, w.baseline.cell)
        result = rebase_metadata(w, current, profile)
        self.save(result)
        return result, profile

    def upgrade_mpp(self, w):
        """Explicit, read-only adoption of native MPP ownership; keeps drafts."""
        from .metadata_upgrade import rebase_mpp
        from .permissions import apply_profile_permissions

        current, profile = self.read(w.baseline.library, w.baseline.cell)
        raw = self.native.read(w.baseline.library, w.baseline.cell)
        flat, _ = decode(raw[:8])
        flat = apply_profile_permissions(flat, profile)
        result = rebase_mpp(w, flat, current, profile)
        self.save(result)
        return result, profile

    def _commands(self, w, current):
        out = []
        for op in w.operations:
            d = {"action": op.action, "uid": op.uid}
            if op.action in ("update", "delete", "transform"):
                d["before"] = current.objects[op.uid].native_row
            if op.action in ("delete", "transform") and current.objects[op.uid].mpp:
                d["mpp_before"] = current.objects[op.uid].mpp.native_definition
            if op.shape:
                d["shape"] = shape_payload(op.shape)
            if op.placement:
                d["placement"] = {
                    "origin": list(map(str, op.placement.origin)),
                    "orientation": op.placement.orientation,
                }
            if op.guard:
                d["guard"] = op.guard
            out.append(d)
        return out

    def _payload_hash(self, w):
        return payload_hash(w)

    @staticmethod
    def _rebind_connectivity(workspace, snapshot):
        from .circuit.query import rebind_layout
        return rebind_layout(workspace.layout_connectivity, snapshot)

    def commit(
        self,
        w: Workspace,
        *,
        request_id: str | None = None,
        fail_after: int | None = None,
        scope_region_ids: set[str] | None = None,
        exclude_region_ids: set[str] | None = None,
    ) -> Workspace:
        """Submit one verified batch; never automatically retry a native write.

        Returns a clean Workspace (or partially committed Workspace when scoped) on
        success, or the retained draft on a confirmed rollback. Disjoint outside-region
        edits in Virtuoso are merged automatically; edits overlapping OperationRegions
        fail with VIRTUOSO_EDIT_IN_REGION before modifying native state.
        """
        from .cooperative_sync import (
            analyze_virtuoso_delta,
            needs_native_prepare,
            rebind_operations,
            rebase_workspace_onto_virtuoso,
            require_virtuoso_saved,
            translate_native_unsaved_error,
        )

        self._require_settled(w)
        from .local_models import require_references
        from .editor import projected

        require_references(w.local_data, projected(w))
        if w.validation_only:
            w.validation_scope()
        elif getattr(self.native, "requires_circuit_context", False) or w.circuit:
            from .circuit.query import require_context

            require_context(w)
        if not w.operations:
            return w.model_copy(update={"last_result": {"status": "no_op"}})
        from .routing import require_persistent_routes

        require_persistent_routes(w.baseline)
        from .groups import require_groups

        require_groups(w.baseline)
        request_id = checked_id(str(uuid4()) if request_id is None else request_id)
        expected_hash = self._payload_hash(w)
        journal = TransactionJournal(self.state, request_id)
        previous = journal.check(expected_hash)
        receipt_hash = (
            previous["hash"]
            if previous is not None and previous.get("source_hash") == expected_hash
            else expected_hash
        )
        raw_receipt = self.native.call("ltReceiptRead", request_id)
        receipt = self._receipt(raw_receipt, w, request_id, receipt_hash)
        if receipt:
            return self.reconcile(
                w.model_copy(
                    update={
                        "state": "indeterminate",
                        "last_result": {
                            "request_id": request_id,
                            "hash": receipt_hash,
                            "source_hash": expected_hash,
                        },
                    }
                )
            )
        if previous and previous.get("status") in (
            "prepared",
            "submitting",
            "indeterminate",
        ):
            raise LiteError(
                "RECONCILE_BEFORE_RETRY",
                "该请求有未完成的持久记录，先核对服务器实际状态。",
            )
        current, profile = self._read_cooperative_source(w.baseline.library, w.baseline.cell)
        fast_path = scope_region_ids is None and exclude_region_ids is None
        if fast_path:
            try:
                require_unchanged(w, current)
            except LiteError as exc:
                if exc.code != "SOURCE_CONFLICT":
                    raise
                fast_path = False

        if fast_path:
            if current.dirty and not w.baseline.dirty:
                require_virtuoso_saved(current)
            from .regions.constraints import validate_pending

            validate_pending(w, profile)
            full_rebased_w = w
            tx_w = w
            retained_ops: tuple[Operation, ...] = ()
            tx_hash = expected_hash
            merged_circuit = w.circuit
            merged_connectivity = None
        else:
            analyze_virtuoso_delta(w, current, profile)
            if needs_native_prepare(current):
                try:
                    self.native.call(
                        "ltPrepare", w.baseline.library, w.baseline.cell, uuid4().hex
                    )
                except LiteError as exc:
                    raise translate_native_unsaved_error(exc) from exc
                current, profile, merged_circuit, merged_connectivity = self._read_with_context(w)
            elif w.circuit is not None and hasattr(self.native, "read_pair") and not same_snapshot(w.baseline, current):
                current, profile, merged_circuit, merged_connectivity = self._read_with_context(w)
            else:
                merged_circuit = w.circuit
                merged_connectivity = self._rebind_connectivity(w, current)
            full_rebased_w, tx_w, retained_ops, delta = rebase_workspace_onto_virtuoso(
                w,
                current,
                profile,
                circuit=merged_circuit,
                connectivity=merged_connectivity,
                scope_region_ids=scope_region_ids,
                exclude_region_ids=exclude_region_ids,
            )
            if not tx_w.operations:
                return full_rebased_w.model_copy(
                    update={
                        "last_result": {
                            "status": "no_op",
                            "remaining_operations": len(full_rebased_w.operations),
                            "virtuoso_delta": {
                                "added": len(delta["added"]),
                                "modified": len(delta["modified"]),
                                "deleted": len(delta["deleted"]),
                            },
                        }
                    }
                )
            tx_hash = self._payload_hash(tx_w)

        # Validate again at the adapter boundary: loading or constructing a
        # Workspace directly must not bypass the local editing permissions.
        from .editor import Editor

        validation = Editor(
            Workspace(baseline=tx_w.baseline, profile_id=profile.id), profile
        )
        ordinary = [
            op for op in tx_w.operations if op.action not in ("guard", "delete_guard")
        ]
        validation.apply(ordinary)
        for op in tx_w.operations:
            if op.action == "delete_guard":
                validation.delete_guard(op.uid)
            elif op.action == "guard":
                preview = tx_w.guard_previews.get(op.uid)
                if not preview or preview.get("command_digest") != digest(op.guard):
                    raise LiteError("GUARD_PREVIEW_REQUIRED")
        backup = "COLAYOUT_MVP_LITE_BACKUP_" + uuid4().hex[:12]
        request = {
            "version": "colayout-lite-1",
            "request_id": request_id,
            "hash": tx_hash,
            "counter": current.counter,
            "dependencies": current.dependencies,
            "backup": backup,
            "operations": self._commands(tx_w, current),
            "fail_after": fail_after,
        }
        request["writable_lpps"] = profile.writable_lpps
        record = journal.prepare(tx_w, tx_hash, backup, source_hash=expected_hash)
        pending = w.model_copy(
            update={
                "state": "submitting",
                "last_result": {
                    "request_id": request_id,
                    "hash": tx_hash,
                    "source_hash": expected_hash,
                    "backup": backup,
                },
            }
        )
        self.save(pending)
        try:
            receipt = self._receipt(
                self.native.transact(current.library, current.cell, request),
                tx_w,
                request_id,
                tx_hash,
            )
            if not receipt:
                raise LiteError("MISSING_RECEIPT")
            after, _ = self.read(current.library, current.cell)
            if receipt.status == "saved":
                after = self._verify_saved_readback(tx_w, after, profile)
                after = retain_display(w.baseline, after)
                rebound_retained = (
                    rebind_operations(retained_ops, full_rebased_w.baseline, after)
                    if retained_ops
                    else ()
                )
                retained_uids = {op.uid for op in rebound_retained}
                final_previews = {
                    k: v for k, v in full_rebased_w.guard_previews.items() if k in retained_uids
                }
                result = full_rebased_w.model_copy(
                    update={
                        "baseline": after,
                        "circuit": merged_circuit,
                        "layout_connectivity": self._rebind_connectivity(full_rebased_w, after),
                        "operations": rebound_retained,
                        "guard_previews": final_previews,
                        "revision": w.revision + 1,
                        "state": "editing" if rebound_retained else "clean",
                        "last_result": {
                            "status": "saved_verified",
                            "request_id": request_id,
                            "backup": backup,
                            "remaining_operations": len(rebound_retained),
                        },
                    }
                )
            elif receipt.status == "rolled_back":
                if not same_snapshot(after, tx_w.baseline):
                    raise LiteError("ROLLBACK_MISMATCH")
                result = full_rebased_w.model_copy(
                    update={
                        "baseline": after,
                        "circuit": merged_circuit,
                        "layout_connectivity": self._rebind_connectivity(full_rebased_w, after),
                        "state": "editing",
                        "last_result": {
                            "status": "rolled_back",
                            "request_id": request_id,
                            "backup": backup,
                            "failed_operation": receipt.phase,
                        },
                    }
                )
            else:
                raise LiteError("RECOVERY_REQUIRED", receipt.status)
            record["status"] = result.last_result["status"]
            journal.write(record)
            self.save(result)
            track_temporary(self, current.library, backup, current.cell)
            if result.last_result.get("status") == "saved_verified":
                try:
                    self.native.call(
                        "ltRelease", [current.library + "/" + current.cell]
                    )
                except Exception as release_error:
                    record["resource_release_error"] = str(release_error)
                    journal.write(record)
                if (
                    result.local_data.regions
                    and not result.validation_only
                    and not current.cell.startswith("COLAYOUT_MVP_LITE_")
                    and getattr(self.native, "config_path", None) is not None
                ):
                    try:
                        self.sync_region_guides(result)
                    except Exception as guide_err:
                        result = result.model_copy(
                            update={
                                "last_result": {
                                    **result.last_result,
                                    "guide_sync_warning": str(guide_err),
                                }
                            }
                        )
                        self.save(result)
            return result
        except Exception as exc:
            exc = translate_native_unsaved_error(exc) if isinstance(exc, LiteError) else exc
            record["status"] = "indeterminate"
            error_message = str(exc)
            record["error"] = error_message
            # Preserve the original native/verification error if the local
            # disk also fails. The pre-dispatch submitting record remains.
            for persist in (
                lambda: self.save(
                    pending.model_copy(
                        update={
                            "state": "indeterminate",
                            "last_result": {
                                **pending.last_result,
                                "error": error_message,
                            },
                        }
                    )
                ),
                lambda: journal.write(record),
            ):
                try:
                    persist()
                except Exception as storage_error:
                    exc.add_note(
                        f"Could not persist recovery evidence: {storage_error}"
                    )
            raise exc

    def reconcile(self, w: Workspace) -> Workspace:
        """Resolve an unknown write by validating its receipt AND actual layout."""
        uid = w.last_result.get("request_id")
        if not uid:
            raise LiteError(
                "NO_PENDING_REQUEST",
                "没有待核对的提交回执；若提示服务器差异，请使用“保留草稿另建副本”。",
            )
        expected_source_hash = self._payload_hash(w)
        if w.last_result.get("source_hash", w.last_result.get("hash", expected_source_hash)) != expected_source_hash:
            raise LiteError(
                "RECEIPT_MISMATCH", "未决草稿内容已改变；请加载原持久工作区。"
            )
        tx_hash = w.last_result.get("hash", expected_source_hash)
        journal = TransactionJournal(self.state, uid)
        record = journal.check(expected_source_hash)
        tx_w = w
        if tx_hash != expected_source_hash and journal.workspace_path.exists():
            tx_w = Workspace.model_validate_json(
                journal.workspace_path.read_text(encoding="utf-8")
            )
        self._receipt(self.native.call("ltReceiptRead", uid), tx_w, uid, tx_hash)
        actual, profile = self.read(tx_w.baseline.library, tx_w.baseline.cell)
        try:
            matches = verify(tx_w, actual, profile=profile)
        except LiteError:
            matches = False
        if matches:
            actual = retain_display(w.baseline, actual)
            commit_uids = {op.uid for op in tx_w.operations}
            retained = tuple(op for op in w.operations if op.uid not in commit_uids)
            if retained:
                from .cooperative_sync import rebind_operations

                retained = rebind_operations(retained, w.baseline, actual)
            retained_uids = {op.uid for op in retained}
            result = w.model_copy(
                update={
                    "baseline": actual,
                    "layout_connectivity": self._rebind_connectivity(w, actual),
                    "operations": retained,
                    "guard_previews": {
                        k: v for k, v in w.guard_previews.items() if k in retained_uids
                    },
                    "state": "editing" if retained else "clean",
                    "revision": w.revision + 1,
                    "last_result": {
                        "status": "saved_verified",
                        "reconciled": True,
                        "request_id": uid,
                    },
                }
            )
        elif same_snapshot(actual, tx_w.baseline):
            result = w.model_copy(
                update={
                    "baseline": actual,
                    "layout_connectivity": self._rebind_connectivity(w, actual),
                    "state": "editing",
                    "last_result": {
                        "status": "not_applied_or_rolled_back",
                        "request_id": uid,
                    },
                }
            )
        else:
            raise LiteError(
                "INDETERMINATE_COMMIT",
                "Actual target matches neither baseline nor intended result",
            )
        if record is not None:
            record.update(status=result.last_result["status"], reconciled=True)
            record.pop("error", None)
            journal.write(record)
        self.save(result)
        return result

    @serialized_adapter
    @temporary_scope
    def guard_preview(
        self,
        editor,
        *,
        bbox=None,
        points=None,
        template,
        net_intent=None,
        _validation=False,
        progress=None,
    ):
        progress=progress or (lambda message:None)
        if not _validation:
            from .guard_ports import required_net
            net_intent=required_net(net_intent)
        progress('准备保护环与工艺模板…')
        editor.check_revision(None)
        grid = editor.profile.grid
        if (bbox is None) == (points is None):
            raise LiteError("MPP_NEEDS_BOX_OR_PATH")
        if bbox:
            (x, y), (xx, yy) = bbox
            if x >= xx or y >= yy:
                raise ValueError("Guard area must be positive")
            mid = ((x + xx) // (2 * grid)) * grid
            path = [(mid, y), (xx, y), (xx, yy), (x, yy), (x, y), (mid, y)]
        else:
            from .model import Shape

            path = [tuple(pt) for pt in points]
            Shape(kind="path", lpp=("validation", "drawing"), points=path, width=2)
            if path[0] == path[-1]:
                raise LiteError("OPEN_MPP_REQUIRED")
            if any(a[0] != b[0] and a[1] != b[1] for a, b in zip(path, path[1:])):
                raise LiteError(
                    "MPP_ORTHOGONAL_REQUIRED", "新建MPP目前支持水平、竖直及90°转角。"
                )
        if any(type(v) is not int or v % grid for p in path for v in p):
            raise LiteError("OFF_GRID")
        w = editor.w
        recipe = None
        if template.get("schema_version") == "colayout-mpp-recipe-2":
            from .guard_recipes import configured

            recipe = configured(editor.profile, template, validation=_validation)
            compiled_recipe = recipe.payload(bbox, grid, net_intent, points=points)
        elif template not in editor.profile.guard_templates:
            raise LiteError("UNKNOWN_GUARD_TEMPLATE")
        else:
            from .mpp_recipe import native_templates
            if template not in native_templates(editor.profile):
                raise LiteError("GUARD_SAMPLE_REQUIRED", "双排模板需先从用户样例建立已验证配方。")
        current, current_profile = self._read_cooperative_source(
            w.baseline.library, w.baseline.cell
        )
        if w.validation_only:
            require_unchanged(w, current)
            rebased_w = w
        elif current.dirty or not same_snapshot(w.baseline, current):
            from .cooperative_sync import (
                analyze_virtuoso_delta,
                needs_native_prepare,
                rebase_workspace_onto_virtuoso,
                translate_native_unsaved_error,
            )

            analyze_virtuoso_delta(w, current, current_profile)
            if needs_native_prepare(current):
                try:
                    self.native.call(
                        "ltPrepare", w.baseline.library, w.baseline.cell, uuid4().hex
                    )
                except LiteError as exc:
                    raise translate_native_unsaved_error(exc) from exc
                current, current_profile = self.read(w.baseline.library, w.baseline.cell)
            rebased_w, _, _, _ = rebase_workspace_onto_virtuoso(
                w, current, current_profile
            )
        else:
            rebased_w = w
        factory=self.create_validation_workspace if rebased_w.validation_only else self.create_workspace
        temporary, profile = factory(
            current.library, current.cell, label="PREVIEW"
        )
        progress('准备当前版图副本…')
        if rebased_w.operations:
            from .clone_draft import bind
            temporary = bind(rebased_w,temporary)
            temporary = self.commit(temporary)
        uid = str(uuid4())
        spec = (
            compiled_recipe
            if recipe
            else {
                "technology_library": template["library"],
                "template": template["name"],
                "definition": template["definition"],
                "points": [[str(a), str(b)] for a, b in path],
                "net_intent": net_intent,
            }
        )
        op = Operation(action="guard", uid=uid, guard=spec)
        # First native preview cannot be predicted from geometry; execute the
        # same native transaction, then capture exact members for commit checks.
        request_id = str(uuid4())
        backup = "COLAYOUT_MVP_LITE_BACKUP_" + uuid4().hex[:12]
        progress('正在 Virtuoso 中生成保护环…')
        self.native.transact(
            temporary.baseline.library,
            temporary.baseline.cell,
            {
                "version": "colayout-lite-1",
                "request_id": request_id,
                "hash": digest(spec),
                "counter": temporary.baseline.counter,
                "dependencies": temporary.baseline.dependencies,
                "backup": backup,
                "operations": [{"action": "guard", "uid": uid, "guard": spec}],
                "fail_after": None,
            },
        )
        receipt = self.native.call("ltReceiptRead", request_id)
        if not receipt or receipt[3] != "saved":
            raise LiteError("GUARD_PREVIEW_FAILED")
        track_temporary(self,temporary.baseline.library,backup,temporary.baseline.cell)
        progress('接收并检查保护环几何…')
        after, _ = self.read(temporary.baseline.library, temporary.baseline.cell)
        members = {
            k: o.model_dump() for k, o in after.objects.items() if k == uid and o.mpp
        }
        if not members:
            raise LiteError("EMPTY_GUARD")
        # All existing objects and child masters must remain unchanged.
        existing = after.model_copy(
            update={"objects": {k: o for k, o in after.objects.items() if k != uid}}
        )
        if existing.semantic() != temporary.baseline.semantic():
            raise LiteError("GUARD_CHANGED_NEIGHBOURS")
        previews = {
            **w.guard_previews,
            uid: {
                "objects": members,
                "display": after.display,
                "cell": after.cell,
                "command_digest": digest(spec),
            },
        }
        editor.publish(
            w.model_copy(
                update={"operations": w.operations + (op,), "guard_previews": previews}
            )
        )
        self.save(editor.w)
        return uid
