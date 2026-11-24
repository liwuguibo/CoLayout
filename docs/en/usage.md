# Using CoLayout

[简体中文](../zh/usage.md) | **English** · [Project home](../../README.en.md) · [Documentation](index.md)

## The workspace

The APP keeps one authoritative local document. Humans and API clients see the same objects, region hierarchy, UID identities and undo semantics. A local operation region is an organizational boundary, not a new Virtuoso cell.

After the process has been approved, use the File menu to import the intended native Layout and its corresponding schematic reference. Native Layout PIN/network data defines physical targets; schematic information helps understand topology, matching and design intent. Import is explicit.

Create an operation region around the objects to work on, inspect its interfaces and give its ID to a layout agent. An interface missing physical access can remain a declared obligation; drawing a guard or connection later must satisfy it. A virtual marker does not create metal.

## A typical collaboration

1. A human selects the source design and partitions local operation regions. CoLayout automatically renders non-fabricating warning boundaries (`[CoLayout AI Region: ...] DO NOT EDIT INSIDE`) in Virtuoso so human edits avoid AI-owned footprints.
2. Each layout agent claims one assigned region. Sibling agents can proceed in parallel; a parent manages only direct child shells and interfaces. Meanwhile, humans can continue editing outside all operation regions in the full Virtuoso GUI.
3. Build the interior, using directional PIN mode where appropriate. Complete device access, routing and sampled guard structures.
4. Tighten the operation region with the property-panel action/API, select fixed PIN mode and complete real boundary exits.
5. Run local checks frequently and inspect warnings. Run server DRC when appropriate, review active errors and finish only with valid whole-region evidence from both.
6. Push verified work explicitly to Virtuoso (via the toolbar **写入** / `workspace sync` / `release_region(sync_to_virtuoso=True)`), without waiting for unrelated busy regions to finish, then perform full-chip checks/LVS and final sign-off.

During agent occupancy, physical human edits inside CoLayout are locked for that construction level. Navigation remains available; positioning guides are the limited collaboration exception. Do not terminate another client's session to bypass a lock. When editing in the Virtuoso GUI, keep changes outside all operation-region warning frames and press `F2` to save before syncing.

## Decoupled Virtuoso and CoLayout collaboration

CoLayout decouples human editing in the Virtuoso GUI from AI/region editing inside local `OperationRegion`s. Whenever both sides' edits remain spatially disjoint, either side can pull or push immediately without losing drafts or blocking busy sibling regions:

- **Automatic Virtuoso region warning guides**: Creating, resizing/tightening or dissolving an `OperationRegion` in the UI or through the Agent API (`workspace create_region` / `dissolve_region`) automatically updates non-fabricating warning guides in Virtuoso via transient highlight sets (`geCreateHilightSet`, rendering `marker/warning` + `y0/drawing` boxes and `[CoLayout AI Region: <name/id>] DO NOT EDIT INSIDE` labels). These highlights do not alter OA database shapes, Layout XL extraction state or `modifiedButNotSaved`, and never enter CoLayout snapshots or Calibre verification decks. You can also trigger a manual redraw with `workspace sync_regions`.
- **Pull outside-region Virtuoso edits anytime (`读取` / `workspace pull`)**: After saving (`F2`) outside-region edits in Virtuoso, click **读取** (`文件 → 读取服务器`) or call `workspace(action="pull")` to merge them into the local baseline. Unlike a full workspace replacement, cooperative pull **preserves pending local `operations` and untouched active Agent region leases**. (To discard local work and replace the entire workspace from Virtuoso, use `文件 → 从 Virtuoso 重新导入…`, which still requires idle sessions.)
- **Push finished non-leased regions anytime (`写入` / `workspace sync` / `release_region`)**: Clicking **写入** (`文件 → 写入 Virtuoso`), calling `workspace(action="sync")` (optionally scoped to one finished `region_id`), or releasing with `release_region(outcome="done", sync_to_virtuoso=True)` merges Virtuoso's latest outside-region layout and overwrites the finished region(s) purely from their **final reviewed snapshot by `colayoutObjectId`** without replaying intermediate edit history (untouched wires/`oaRoute`s remain intact, instances update pose in place preserving Layout XL bindings, and removed/modified/added wires, standard vias and guard rings are overwritten directly), then saves and redraws Virtuoso while retaining in-progress sibling drafts and active leases locally.
- **Safety guards (`VIRTUOSO_UNSAVED_CHANGES` & `VIRTUOSO_EDIT_IN_REGION`)**:
  - `VIRTUOSO_UNSAVED_CHANGES`: Cooperative pull/sync refuses while Virtuoso reports unsaved in-memory edits (`modifiedButNotSaved=t`). Save (`F2`) or revert in Virtuoso and retry.
  - `VIRTUOSO_EDIT_IN_REGION`: If Virtuoso added, moved/modified or deleted any element overlapping an `OperationRegion`, both pull and sync abort without mutating either side and list the offending object names, kinds, layers, `µm` coordinates and region names. Delete or move those elements out of the region in Virtuoso, save (`F2`), and retry.

## Metal pour

The top-level Rectangle button opens a secondary menu containing Rectangle and 铺铜 side by side. Pour fills a dragged rectangle on a selected metal layer with a named net intent. Its layer drop-down shares the rectangle tool's LPP presentation/default behavior but lists only writable metal drawing layers. Select direct shapes/vias to connect before opening the panel, if needed. The clearance defaults to the largest configured local spacing rule for that layer, or 0.2 µm when no rule exists, and can be adjusted for the process. The panel's 45° cornering checkbox is off by default. Without targets, all independent islands remain; with targets, disconnected islands are removed. A known conflicting target net, out-of-region bounds or incomplete geometry rejects the whole operation.

Pour avoids every same-layer object not explicitly selected as a target and respects keepouts and blackbox routing policy. In the default mode, non-orthogonal obstacles receive conservative axis-aligned covers, and results are ordinary editable polygons with horizontal/vertical edges only, including concave L and U shapes. Holed regions are orthogonally sliced and merged back into simple polygons without holes. Enabling 45° cornering permits 45°/90° edges; supported 45° diagonal obstacles are avoided using their actual 45° contours, while obstacles at unsupported angles still receive conservative axis-aligned covers. Output slices are likewise merged into simple polygons without holes. Output is capped at 512 polygons per call, with one undo for the whole batch. Grid rounding adds a small clearance guard. This only enforces clearance from explicit obstacles; it does not replace width, density, antenna, electrical/thermal or complete DRC/LVS verification. Run local and server checks after pouring.

## On-demand local RC parasitic evaluation

The top-level **画线** button opens a nearby tool popover that automatically dismisses once canvas wire capture starts so the viewport remains unobstructed. Wire capture previews uncommitted orthogonal L-bend or single-segment drafts as points are added: press `Space` to flip between horizontal-first L, vertical-first L and single-axis orthogonal posture, press `[` / `]` (or `PageDown` / `PageUp` ) to step down/up the metal stack with automatic per-layer default width and standard-via matching (showing a live via enclosure preview at the turn anchor), and right-click to undo the previous step. Enter/double-click commits once through the existing editor transaction, and Esc discards the draft. It does not require whole-region connectivity or DRC success before allowing incremental routing; existing permission, revision and geometry constraints still apply.

**No unsolicited background computation**: Interactive wire capture (`WireCapture` / `WirePanel`), mouse dragging (`MouseEdit`), autoroute browsing, and commits perform **zero continuous or background RC estimation**, preventing local CPU overhead during routine layout editing.

With a reviewed RC model (analytic coefficients or an optional `surrogate` configuration pointing to local pure-NumPy Geo-Graph weights and stackup specs), both humans and agents can **evaluate RC parasitics selectively and on-demand**:
- **Post-routing inspection**: After committing a route in `WirePanel`, click **“查看本次走线 RC”** to open the **RC 寄生影响** tool tab and evaluate the newly routed geometry (`ΔR / ΔCg / ΔCc` relative to the before-route baseline).
- **On-demand panel evaluation & clearing**: In the **RC 寄生影响** panel (`View → RC 寄生影响`), select objects, trace a net, or preview an autoroute candidate, optionally adjust **近邻范围** (`µm`), and click **“计算当前 RC”** to compute on demand or **“清除”** to clear results and canvas highlights. Subsequent edits mark existing results stale (`版图已更新（未自动重算）· 点击“计算当前 RC”更新`) rather than continuously recomputing.

`Ca/Cp` never includes the explicitly enumerated neighbour effect: the same physical component is not coupled to itself, each pair is counted once, and unknown nets remain separate from different nets. When a via-aware neural surrogate is configured, dropping vias across pre-drawn bottom/top traces merges their net connectivity, cancels the prior inter-layer coupling, and evaluates 3D nodal resistance; otherwise arbitrary polygons, parallel branches and unclear topology do not receive a port-equivalent resistance or an RC delay. All interactive estimates run locally from frozen geometry and approved coefficients, without SSH, Virtuoso, Calibre or network calls. Blackboxes contribute only filtered public PIN geometry, never their internal shapes. Unsupported model ranges, missing coefficients and excessive geometry are shown explicitly and never block normal editing.

**Shared Human + Agent RC Inspection API**: The Human UI and the Agent API call the exact same domain service (`evaluate_parasitics_outcome` / `evaluate_parasitics`). Agents use `inspect_region(kind="parasitics")` or CLI (`api.py inspect parasitics`) on demand. This tool provides early design guidance (`signoff: false`) and does not replace DRC, LVS, or Calibre sign-off.

## Layer palette, ruler and collaboration helpers

- **Layer palette (LSW)**: Toggle **图层** in the top bar to open the compact left-column layer palette showing colour swatches for metals, via cuts and device layers. Checkboxes hide/show layers; single-clicking a writable layer syncs the active layer in Wire / Rectangle / Pour tools; **double-clicking a row enters Solo mode** (retaining connecting via cuts for metals), and double-clicking again or clicking **退出独显** restores the previous visibility state. Layer hiding is strictly view-only: hidden layers do not intercept clicks or marquee selection and never affect DRC, sync or Agent snapshots.
- **Interactive ruler**: Click **标尺** or press `K` to measure distances snapped to the manufacturing grid and nearby visible geometry edges/corners (hold `Shift` for grid-only snap). Press `Space` to toggle orthogonal vs. diagonal ΔX/ΔY measurement, click two points to place a ruler, and press `Shift+K` to clear rulers in the current scope. Rulers are session-local viewport overlays and never mutate workspace history.
- **Footer live readout and wire lock pill**: The right side of the footer displays live grid-snapped micron coordinates together with relative `Δ(ΔX, ΔY)` and length/box dimensions during active wire, ruler, box or drag gestures. The **锁线 / 移线** pill toggles directly between inspect-safe conductor lock and movable wires/vias.
- **Copy task context**: Use **编辑 → 复制任务上下文** (or right-click an operation region) to copy a structured summary of the target region ID, bounds, interface PINs and current-scope markups/keepouts to the clipboard for pasting into a layout Agent conversation.

## Hierarchy and routing windows

Ordinary leaf PCells participate in local/server checks. True native hierarchical objects remain blackboxes. Their public PINs may be inside their footprint: object-specific, layer-specific routing windows can open a limited corridor. These do not reveal/edit internal geometry or waive another object's keepout.

Local child operation regions are separate from native hierarchy. Their internal work is controlled by their own agent. Moving a parent-managed child shell transforms its contents coherently and invalidates dependent work as required.

## Checks have different meanings

The process editor's drawing/layers page includes “允许 Agent 使用算法自动布线”, disabled by default, including legacy settings. Only a human save/approval enables route_region. Human UI autorouting and other Agent tools such as arrange, align and automatic vias remain available.

Tightening reserves a small channel around contents and direct-child PIN extents: half the largest PIN dimension per axis plus about 10% (at least one manufacturing grid step). Human UI and API share this behavior, preserve PIN mode/native geometry and only shrink within the original frame, without extra warnings for an already tight frame.

- Local connectivity checks exposed targets and supported material connectivity, not the full extracted device circuit.
- Local DRC includes configured simple geometry, design advice, cross-device Poly conflict checks, configured MIM-region via protection and internal-metal contact advice. Unsupported width geometries are skipped.
- The internal-metal contact warning is nonblocking advice. Known local physical errors and incomplete/stale/truncated results still prevent completion.
- Server DRC uses a private native copy containing the current local edits. It applies the approved process report policy and retains the original records.
- Passing both region checks is not full-chip DRC or LVS sign-off.

Do not change device parameters to hide a mismatch or invent a waiver. If a cooperative pull/sync aborts with `VIRTUOSO_EDIT_IN_REGION`, remove or move the intruding shapes out of the operation region in Virtuoso and save (`F2`) first; if a dependency mismatch or pending-transaction baseline conflict occurs, reconcile the actual native change before synchronizing.

When reviewing local rules, source/note are evidence and descriptions, not executable regional exceptions or electrical conditions. Useful simplified checks should normally remain enabled even when omitted conditions can cause limited false positives; review the stated simplification and possible missed cases. Disable rules only when evidence is insufficient, the algorithm cannot perform the check, or repeated false positives on legal basic structures would make editing impractical. A note does not implement an omitted condition, and full server DRC remains necessary.

Local `spacing` does not distinguish same-net/different-net geometry or compare net names; a rule ID does not select conditions from the original deck. Enabled spacing rules on the same layer all apply, so satisfying all of them within check coverage requires the largest enabled threshold, not a conditional choice. Configure an evidence-backed simplification for the selected PDK and describe its actual behavior, original evidence and omitted cases; passing configuration checks does not establish the original conditions. Layout agents must not infer extra capabilities from rule names/notes or change process settings to bypass checks.

## Save, reopen and recovery

The APP automatically maintains its internal working document and recovery records. Explicit Save / Save As creates or updates the user's portable JSON archive. Later editing does not continuously rewrite that archive.

The Agent workspace save API creates a standalone snapshot; load follows the same semantics as opening a saved workspace in the UI, with a backup first. Loading replaces the whole current document and requires idle sessions/jobs and no unresolved native transaction. It does not synchronize to Virtuoso.

For an unknown request outcome, preserve the original request ID/body and pending file. Query/reconcile it; do not send a new create request. For an uncertain native write, preserve the receipt and use the recovery flow. Never remove evidence to make an error disappear.

## Common controls

| Default | Action |
|---|---|
| F | Fit the view |
| K / Shift+K | Toggle ruler capture / clear rulers in current scope |
| Space (in wire or ruler) | Flip wire L-bend posture / toggle orthogonal vs. diagonal ruler |
| [ / ] (in wire) | Step down / up the metal stack and auto-match standard via |
| Ctrl+E | Return to the parent level |
| Ctrl+R | Rotate selected supported objects by 90° |
| Ctrl+Z or U | Undo |
| Ctrl+Y or Ctrl+Shift+Z | Redo |
| Ctrl + wheel | Change the hierarchy peek level |
| Esc | Cancel the current interaction |

Shortcuts are configurable in the UI. Text fields keep text-editing behavior. Panel buttons toggle their corresponding panels; auto-routing and RC parasitic estimates are available through View rather than the main toolbar.

---

[简体中文](../zh/usage.md) | **English** · [Project home](../../README.en.md) · [Documentation](index.md)
