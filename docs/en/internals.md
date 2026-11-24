# Source implementation guide

[简体中文](../zh/internals.md) | **English** · [Project home](../../README.en.md) · [Documentation](index.md)

This complements the [architecture](architecture.md) with actual call chains and change locations. File links open source; names identify symbols rather than frozen line numbers.

## 1. Boot, ownership and shutdown

On Windows, [tools/project.ps1](../../tools/project.ps1) uses pythonw; on Linux, `tools/project.sh` uses `.venv/bin/python`. Both read .colayout/installation.json and launch `-m colayout_lite.workspace_app` with the document, --installation and --agent-api, detecting an existing process for that pointer. The document lock remains the final writer guard. Linux workers use separate process groups and Windows uses Job Objects so close/cancellation covers descendants.

[workspace_app.main / WorkspaceWindow](../../colayout_lite/workspace_app.py) installs diagnostics, constructs QApplication and StudioWindow, restores the internal document and pending native state, then enables AgentHub. [StudioWindow](../../colayout_lite/studio_window.py) composes the shell around WorkspaceWindow; [ui.Window](../../colayout_lite/ui.py) is the shared canvas/controller base, not the recommended standalone user entry.

WorkspaceWindow.lock_path/replace_lock manage QLockFile and installation/document rebinding. save_local persists the shared Editor. AgentHub reads archive/import roots at construction, so changing the active client archive root requires an idle restart. [AgentHub.close](../../colayout_lite/agent_tools/app.py), window close handlers and task controllers own their timers, workers and subscriptions. A dock close is not a request to cancel its job.

## 2. Data and serialization

| Type / entry | Meaning |
|---|---|
| [Shape / Placement / Object](../../colayout_lite/model.py) | Exact geometry, pose, UID, native facts and supported editing capability |
| Snapshot | Source library/cell/session/counter, dependencies, objects, PINs, display, routes and groups |
| Operation | create/update/delete/transform/guard/delete_guard; existing objects require a before fingerprint |
| Workspace | Baseline, operations, local_data, circuit reference, physical targets, revision, state and last_result |
| [LocalData](../../colayout_lite/local_models.py) | Local regions, interfaces, annotations, locator/keepout data and native routing windows |
| [TechnologyProfile](../../colayout_lite/technology.py) | Units/grid, material roles, vias, writable LPPs, defaults and sampled guard capabilities |
| [ProcessPackage](../../colayout_lite/process_package.py) | Technology + DRC + report policy + sample evidence, submitted as one reviewed configuration |

Record forbids unknown fields and non-finite values and is frozen at the model level. Nested dictionaries/lists are not magically immutable: never mutate a published nested container in place. Build replacement values and publish through Editor; caches and asynchronous snapshots rely on stable input identity.

Three different quantities must remain distinct: local Workspace.revision, native Snapshot.counter/session, and scope_state.fingerprint. The native counter is not a local edit version. A scoped fingerprint covers relevant physical/interface inputs, not every unrelated sibling edit.

[document.py](../../colayout_lite/document.py) writes colayout-document-1 with embedded workspace and technology. load_document checks schema, technology identity, references and region constraints, then performs the explicit edge-PIN migration. [storage.py](../../colayout_lite/storage.py) also supports internal workspace files with baseline_ref; copying such a bare file alone may omit its baseline store. Use the portable document/save API for exchange.

[WorkspaceStorage](../../colayout_lite/workspace_storage.py) separates the internal working copy from the external archive, records document-source.json, and keeps recovery backups internally. Opening/restoring must not turn a protected recovery file into the Save destination. Unknown native state has its own persisted copy; [sync_recovery.py](../../colayout_lite/sync_recovery.py) reconciles it with the document on startup.

### Data-format and compatibility index

These are current formats/entry points, not instructions to invent a migration. Prefer APP save/open, process export/import and the public client; never force loading by editing a version tag.

| Category/version | Reader/writer | Compatibility and delivery boundary |
|---|---|---|
| colayout-document-1 | document.document_text / save_document / load_document | Portable workspace embeds workspace + technology; other outer versions are rejected. Loading also checks identity, references and region constraints |
| colayout-lite-workspace-1; colayout-local-data-1 | model.Workspace; local_models.LocalData | Internal models, not standalone complete archives; exchange through the portable document |
| colayout-lite-storage-1 / baseline_ref | storage.save_workspace / load_baseline; Workspace.baseline_reference | Internal optimization; a baseline_ref record may omit storage_version but its reference must resolve. Copying only the record can lose its baseline |
| colayout-lite-layout-1…6 (model default 6) | model.Snapshot; codec.decode accepts colayout-lite-native-1…6 protocols | Parsing an old tag does not supply missing evidence; keep snapshots within the workspace, not as proof of edit capability |
| colayout-circuit-2/3/4; colayout-layout-connectivity-1 | circuit.models / circuit.codec | Reference/physical-target caches; v4 additionally validates hierarchy evidence. Not standalone rebindable designs |
| colayout-lite-technology-1 | technology.TechnologyProfile | Existing MPP recipe migration runs on read, followed by consistency/capability checks. Share a unified process package, not a local registry |
| colayout-process-package-1; outer colayout-process-file-1 | process_package.encode / decode; ProcessConfiguration.export | Outer checksum and format are checked; import requires local binding and human review, not another user's runtime credentials |
| colayout-drc-profile-1; colayout-drc-result-1 | drc.models / config / job_storage | Settings belong to the process package; results belong to a stamped/scoped job. Missing legacy MIM data becomes unconfigured, not unsupported |
| colayout-agent-connection-2; colayout-agent-api-2 / colayout-agent-response-2 | agent_tools.http_api / session / client | Current client accepts only v2 descriptors and rejects Bridge/v1 descriptors; update through publication, not edited tokens/version tags |
| colayout-installation-1; colayout-customer-runtime-1 | bootstrap and client generation/publication tools | Host-specific installation/executor paths, not shareable workspace/process files |

edge_pin_migration.normalize handles legacy interior PINs: where applicable it preserves a source backup before moving to a legal edge. submitting/indeterminate/conflict workspaces skip this migration, and impossible placement fails explicitly. Compatibility is not permission to bypass pending state or evidence checks.

Recommended entry points remain workspace_app/the launcher for the human workspace, and process_agent plus unified process_configuration for proposals. Compatibility imports such as process_dialog.py and older helpers do not replace those workflows.

## 3. A human edit and an API edit

Human input flows through [MouseEdit](../../colayout_lite/mouse_edit.py), [HumanEdit](../../colayout_lite/human_edit.py), Actions/region services and Editor. Properties and shortcuts must call the same domain actions rather than directly editing graphics-item coordinates.

API flow:

~~~text
AgentHTTPServer POST /call
  -> QtDispatcher queue / owner-thread execution
  -> AgentSession.invoke[_async] / audit / contract validation
  -> RegionLeases.lease + actor
  -> region_edits.edit_region / apply_operation
  -> Actions / RegionActions / layout_helpers.service
  -> Editor.transaction -> Editor.publish
  -> save_local + audit response
~~~

[QtDispatcher](../../colayout_lite/agent_tools/http_api.py) has bounded admission and yields between queued calls. request_not_started applies to that HTTP request only; a timed-out status query does not prove that the original edit job did nothing. OUTCOME_UNKNOWN after execution must retain the original request identity.

[AgentSession](../../colayout_lite/agent_tools/session.py) performs idempotent audit lookup before dispatch. [RegionLeases.actor](../../colayout_lite/agent_tools/leases.py) can accept an older observed revision only when its recorded scope fingerprint still matches. Do not replace this with either unconditional acceptance or global-revision equality.

[Editor.transaction](../../colayout_lite/editor.py) stages ordered operations and publishes once only if its source is unchanged. publish validates region constraints, references and ownership, creates one undo step and notifies views. A new native baseline is an undo-history boundary. Agent-owned undo uses history tokens, not a second undo stack.

Metal pour uses the same domain semantics for the UI and Agent: create_pour's allow_45 parameter and the human 45° cornering checkbox are both off by default. In that mode, non-orthogonal obstacles receive conservative axis-aligned covers, output has horizontal/vertical edges only, and orthogonal slices are merged into simple polygons without holes, including concave L/U shapes. When enabled, 45°/90° edges are permitted; supported 45° diagonal obstacles are avoided using their actual 45° contours, while unsupported angles still receive conservative axis-aligned covers. Output slices are likewise merged into simple polygons without holes. The UI and Agent must not use different defaults or obstacle contours.

## 4. Parallel workers and adoption

[batch_edits.py](../../colayout_lite/agent_tools/batch_edits.py) sends eligible large batches to [batch_worker.py](../../colayout_lite/agent_tools/batch_worker.py); prepare_local reuses ordered domain validation. [edit_adoption.adopt_edit](../../colayout_lite/agent_tools/edit_adoption.py) verifies cancellation, lease/current inputs and then calls workspace_delta.merge followed by Editor.publish.

For example, sibling B can finish while a worker computes A. Its result must merge A's changes into the current workspace and preserve B. If A's frame/PIN/pose or relevant parent input changed, the old result must be rejected. Never install the worker's entire old Workspace as current state.

[read_pool.py](../../colayout_lite/agent_tools/read_pool.py) handles bounded read processes, frozen snapshots and cached responses for heavy net/screenshot queries. Capture's stamp also includes visible context and overlays. The owner thread validates and audits the result. Read workers cannot publish edits.

[JobRegistry](../../colayout_lite/agent_tools/jobs.py) tracks queued/running/completed/failed/cancelled tasks and invokes adoption on the owner thread. Job completion, edit_applied and current evidence are different facts. [native_lane.channel](../../colayout_lite/native_lane.py) serializes native operations by Bridge environment/project within the installation's lock root; it is not a server-wide lock across unrelated clones. Do not run independent installations against the same writable native target concurrently.

## 5. Native synchronization state

The human [SyncController](../../colayout_lite/presentation/synchronization.py) prepares UI/backups/tasks; [Adapter.cooperative_pull / commit / reconcile](../../colayout_lite/adapter.py) and [cooperative_sync.py](../../colayout_lite/cooperative_sync.py) own cooperative merging and native correctness.

- **Virtuoso region warning guides**: [cooperative_sync.py](../../colayout_lite/cooperative_sync.py) (`region_guide_signature`, `build_region_guides`, `ascii_guide_label`) encodes local `OperationRegion`s and calls [cooperative.il](../../colayout_lite/skill/cooperative.il) (`ltSyncRegionGuides`, `ltApplyGuideHilights`, `ltClearGuideHilights`) to render non-fabricating warning frames via Virtuoso transient highlight sets (`geCreateHilightSet` / `geAddHilightRectangle` / `geAddHilightLabel`) plus `ltRedrawCellView`, avoiding OA database writes, `modifiedCounter` bumps or Layout XL extraction dirtying (`COLAYOUT_MVP_LITE_*` temporary cells are also skipped). `ltClearRegionGuides` remains active for stripping legacy `colayoutRegionGuide` DB figures during clones/commits, while [read.il](../../colayout_lite/skill/read.il) (`ltIsRegionGuide`) and display captures ignore them.
- **Cooperative pull (`Adapter.cooperative_pull`)**: `Adapter._read_cooperative_source` auto-runs `ltPrepare` if split/copied routes or groups raise `DUPLICATE_ROUTE_ID` / `DUPLICATE_FIGGROUP_ID`; it then checks saved Virtuoso state (`require_virtuoso_saved` -> `VIRTUOSO_UNSAVED_CHANGES`), runs `ltPrepare` for unprepared shapes (`needs_native_prepare`), aborts if Virtuoso changes overlap any `OperationRegion` (`analyze_virtuoso_delta`, checking objects and `route.members` -> `VIRTUOSO_EDIT_IN_REGION`), validates untouched active leases via [WorkSessions.validate_cooperative_baseline](../../colayout_lite/work_sessions.py), and installs the rebased baseline via [WorkspaceController.install_cooperative](../../colayout_lite/presentation/workspace_controller.py) / [Editor.adopt_cooperative_baseline](../../colayout_lite/editor.py) while retaining pending `operations`.
- **Incremental cooperative commit & guard preview (`Adapter.commit` / `Adapter.guard_preview`)**: `commit` supports `scope_region_ids` and `exclude_region_ids`. When Virtuoso changed disjointly outside all regions, it three-way rebases via `rebase_workspace_onto_virtuoso`, journals pending state, commits via `ltEnsureEditable` + `ltTransaction` + `dbSave` + `ltRedrawCellView`, validates readback, refreshes highlight guides only on non-temporary target cellviews (excluding `COLAYOUT_MVP_LITE_*` and `validation_only`), and preserves deferred sibling drafts/leases locally. `guard_preview` likewise rebases disjoint outside-region Virtuoso changes before preparing its temporary `PREVIEW` clone while preserving the editor's active-lease baseline.

| Workspace state | Interpretation |
|---|---|
| clean | No pending native operations; local-only organization can still exist |
| editing | Local draft operations retained |
| submitting | Request and pre-dispatch workspace persisted; native outcome may still be unknown |
| indeterminate | Dispatch/receipt/readback did not establish a safe final result |
| conflict | Source or workflow conflict requiring explicit resolution |

commit checks settled state, references, source/dependencies and pending geometry; prepares a TransactionJournal and backup identity; persists submitting; calls the native transaction; validates receipt and actual saved geometry. Only verified success adopts the new baseline and clears operations. A confirmed rollback retains the draft. Exceptions after dispatch retain indeterminate evidence.

MPP subpart enumeration may reorder after reopening a native session. Sync preconditions, readback and rollback/reconciliation compare those parts independent of enumeration order, retaining all fields and duplicate counts. Net enrichment matches parts by their non-net fields and rejects ambiguous matches; existing physical-net proof requirements still apply. Persisted snapshots, request hashes and native transaction preconditions keep their original representation. See [MPP order regressions](../../tests_lite/test_mpp_part_order.py); do not fix a false conflict by clearing pending state or ignoring real part changes.

[TransactionJournal / Receipt](../../colayout_lite/transactions.py) bind request ID, payload hash and target. reconcile compares actual target to intended result and baseline; neither match means INDETERMINATE_COMMIT. A receipt alone is insufficient. Baseline mismatch without a pending transaction must not be “fixed” by clearing a journal.

[NativeTransport](../../colayout_lite/transport.py) connects during construction, lazily loads NATIVE_SCRIPTS and encodes data rather than arbitrary code. Native SKILL files under colayout_lite/skill are CoLayout's protocol implementation; the upstream Python dependency is separately installed. Modify both protocol ends and failure tests together. Keep wire versions, receipt shapes and UID checks compatible.

Paired import permits one explicit unbound Layout case: a native boolean `lxDummy` property is true, the instance has no children, and its nonempty terminals and members all belong to one nonempty native net. `circuit.codec` retains its geometry, UID, properties, terminals and physical nets without inventing a schematic instance or binding. Names, ignore flags and display signal aliases do not establish this exception. Signal-driven dummies, ordinary unbound Layout devices, missing terminal nets and broken binding paths remain rejected; native correspondence (allowing IC618 `INFO (LX-1001)` lines for schematic instances not yet placed in Layout, preserved verbatim in `native_check.report`) and source-stability checks must still pass. Workspace and wire formats stay unchanged, and the reference binding list need not cover these confirmed dummies. [test_native_dummy_import.py](../../tests_lite/test_native_dummy_import.py) covers import, archive restoration, refresh and state preservation on rejection.

## 6. Checks, filters and process review

[RegionTasks](../../colayout_lite/agent_tools/region_tasks.py) creates scoped inputs. [drc/local.run](../../colayout_lite/drc/local.py) performs supported checks; geometry, scope, Poly, MIM and advisory modules remain separate. UI highlighting is not this physical checker.

[drc/jobs.py](../../colayout_lite/drc/jobs.py) persists inputs before launching [drc/worker.py](../../colayout_lite/drc/worker.py). state.json is progress; terminal.json is final evidence written after results. A dead worker with no valid terminal/result is not a pass. [rdb.py](../../colayout_lite/drc/rdb.py) parses server records; [reporting.py](../../colayout_lite/drc/reporting.py) and waivers.py apply the reviewed policy without rewriting raw evidence.

[release_status.py](../../colayout_lite/agent_tools/release_status.py) checks the latest relevant local and server jobs for current/full-region/passed/completion_eligible evidence. Re-run checks after relevant changes; local warnings and filtered server results retain their defined semantics.

PDK flow is process_agent.collect -> check/prepare -> request/submit -> process_configuration proposal -> process_review.approve -> process_transaction.publish_files. [process_center.py](../../colayout_lite/process_center.py) is the UI; process_dialog.py is a compatibility import. process_api.py has no approve endpoint. Native fingerprints, configuration identity and review digests serve different purposes; do not overwrite one with another. New fields must survive validation, UI round-trip and portable export/import.

## 7. Rendering and diagnostics

[Canvas](../../colayout_lite/presentation/canvas.py), [layout_scene.py](../../colayout_lite/presentation/layout_scene.py), owner_graphics.py, view_scope.py, vector_preview.py, [layer_palette.py](../../colayout_lite/layer_palette.py) and [ruler_tool.py](../../colayout_lite/ruler_tool.py) separate interaction, owner/per-layer visibility, vector caches and pure-view rulers. `Window.hidden_layers` and rulers affect only human view rendering and hit filtering; SnapshotCanvas renders scoped screenshots without constructing the main Editor/window and remains unaffected by human layer hiding. Invalidate changed owners and relevant technology/scope; do not equate a display collapse or hidden layer with permission/DRC blackboxing.

[circuit/panel.py](../../colayout_lite/circuit/panel.py), reference_data.py and reference_table.py implement local schematic reference presentation. Diagnose repeated signal connections, owner-thread work, expand/select events and deferred refresh when a modal/reference view freezes.

[diagnostics.install](../../colayout_lite/diagnostics.py) writes Python/Qt/fault logs to artifacts/lite/diagnostics. [UiWatchdog](../../colayout_lite/ui_watchdog.py) writes PID-specific ui-stall stacks when Qt stops responding. Agent runs are beside the current document under agent-runs; native journals follow connection.state_directory; DRC jobs live under artifacts/drc/jobs. Inspect locally and sanitize before publishing.

[Editor.subscribe_commits](../../colayout_lite/editor.py) supplies immutable before/after events without changing the existing changed-UID subscription. RC estimation uses these events for stable deltas. `rc_model_digest` is independent of the native qualification `policy_key`; RC review and optional xRC evidence are separate from native geometry qualification. Blackboxes never enter full extraction for RC; only public PIN Physical rows are appended. Interactive work uses fixed-rate sampling rather than per-event cancellation, with cooperative cancellation and vertex/pair budgets. The M1 long-distance Core-Halo partitioning kernel in [parasitic_partition.py](../../colayout_lite/parasitic_nn/parasitic_partition.py) (`build_core_halo_partitions`, `evaluate_partitioned_capacitance`, zero-double-count `PartitionContribution` shares) and seam edge suppression in [geometry_graph.py](../../colayout_lite/parasitic_nn/geometry_graph.py) (`_is_artificial_seam_edge`) decompose long interconnects across partitions without double counting.

## 8. Change location and existing regression map

Paths in the test column are under tests_lite. These are starting points, not claims of exhaustive coverage.

| Change | Existing tests / additional verification |
|---|---|
| Geometry, transactions, undo | [test_kernel.py](../../tests_lite/test_kernel.py), [test_actions.py](../../tests_lite/test_actions.py), [test_path_outline07.py](../../tests_lite/test_path_outline07.py), [test_path_backtracking.py](../../tests_lite/test_path_backtracking.py) |
| Native commit/reconcile/save | [test_transactions.py](../../tests_lite/test_transactions.py), [test_verified_save_barrier.py](../../tests_lite/test_verified_save_barrier.py), [test_transport_boundary.py](../../tests_lite/test_transport_boundary.py), [test_native_lane.py](../../tests_lite/test_native_lane.py), [test_cooperative_sync.py](../../tests_lite/test_cooperative_sync.py) (warning guides, `VIRTUOSO_EDIT_IN_REGION`, `VIRTUOSO_UNSAVED_CHANGES`, outside-region rebase, active-lease preservation, and scoped region push) |
| Regions, ownership, PINs | [test_regions.py](../../tests_lite/test_regions.py), [test_work_sessions.py](../../tests_lite/test_work_sessions.py), [test_human_edit.py](../../tests_lite/test_human_edit.py), [test_copied_interface_contacts.py](../../tests_lite/test_copied_interface_contacts.py), [test_edge_pin_migration.py](../../tests_lite/test_edge_pin_migration.py) |
| Material/physical checks | [test_conduction_access.py](../../tests_lite/test_conduction_access.py), [test_agent_connectivity.py](../../tests_lite/test_agent_connectivity.py), [test_poly_contact_drc.py](../../tests_lite/test_poly_contact_drc.py), [test_mim_via_check.py](../../tests_lite/test_mim_via_check.py) |
| Internal metal warnings / performance | [test_internal_metal_contacts.py](../../tests_lite/test_internal_metal_contacts.py), [test_drc_performance_equivalence.py](../../tests_lite/test_drc_performance_equivalence.py) |
| Guards and routing windows | [test_mpp_sample_choppability.py](../../tests_lite/test_mpp_sample_choppability.py), [test_guard_ports.py](../../tests_lite/test_guard_ports.py), [test_native_routing_window_router.py](../../tests_lite/test_native_routing_window_router.py) |
| Via helpers / routing | [test_auto_vias.py](../../tests_lite/test_auto_vias.py), [test_via_landing_intent.py](../../tests_lite/test_via_landing_intent.py), [test_layout_helpers.py](../../tests_lite/test_layout_helpers.py), [test_routes.py](../../tests_lite/test_routes.py) |
| Metal pour | [test_pour.py](../../tests_lite/test_pour.py): explicit targets, obstacle clearance, orthogonal output, concave L/U polygons, atomic undo, Agent job adoption, rectangle submenu and Qt dragging |
| Local RC, partitioning and interactive wire drafts | [test_parasitic_rc.py](../../tests_lite/test_parasitic_rc.py), [test_parasitic_neural.py](../../tests_lite/test_parasitic_neural.py), [test_parasitic_partition.py](../../tests_lite/test_parasitic_partition.py), [test_parasitic_process.py](../../tests_lite/test_parasitic_process.py), [test_parasitic_workspace.py](../../tests_lite/test_parasitic_workspace.py), [test_parasitic_view.py](../../tests_lite/test_parasitic_view.py), [test_wire_capture.py](../../tests_lite/test_wire_capture.py), [test_parasitic_agent.py](../../tests_lite/test_parasitic_agent.py): units, deduplication, model scope, E(2) invariance and via-bridged surrogate inference, Core-Halo long-distance partitioning and seam-suppression conservation, blackbox PINs, on-demand lifecycle (zero unsolicited background recompute), post-route deltas and shared Agent `inspect_region(kind="parasitics")` parity |
| Server jobs / parser storage | [test_drc_jobs.py](../../tests_lite/test_drc_jobs.py), [test_drc_job_storage.py](../../tests_lite/test_drc_job_storage.py), [test_drc_hierarchy_scope.py](../../tests_lite/test_drc_hierarchy_scope.py); add parser-specific fixtures for changed report grammar |
| Setup and clients | [test_bootstrap_portability.py](../../tests_lite/test_bootstrap_portability.py), [test_release_boundary.py](../../tests_lite/test_release_boundary.py), [test_client_parallel_replay.py](../../tests_lite/test_client_parallel_replay.py), [test_customer_cli_uid.py](../../tests_lite/test_customer_cli_uid.py), `tests_lite/test_linux_portability.py` |
| PDK collection / configuration | [test_process_setup.py](../../tests_lite/test_process_setup.py), [test_onboarding.py](../../tests_lite/test_onboarding.py); add review/export tests for changed schema |
| GUI/render | [test_translation_cache.py](../../tests_lite/test_translation_cache.py), [test_compact_contrast.py](../../tests_lite/test_compact_contrast.py), [test_gui_streams.py](../../tests_lite/test_gui_streams.py), [test_gui_ergonomics.py](../../tests_lite/test_gui_ergonomics.py); real Qt/pythonw and representative scenes still required |

A new edit action normally touches its shared command/model, permission/scope handling, contract/discovery, synchronous and asynchronous paths, UI adapter and generated client schema. A bug fix should change only the necessary subset and verify parity.

---

[简体中文](../zh/internals.md) | **English** · [Project home](../../README.en.md) · [Documentation](index.md)
