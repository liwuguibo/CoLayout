# Using CoLayout

[简体中文](../zh/usage.md) | **English** · [Project home](../../README.en.md) · [Documentation](index.md)

## The workspace

The APP keeps one authoritative local document. Humans and API clients see the same objects, region hierarchy, UID identities and undo semantics. A local operation region is an organizational boundary, not a new Virtuoso cell.

After the process has been approved, use the File menu to import the intended native Layout and its corresponding schematic reference. Native Layout PIN/network data defines physical targets; schematic information helps understand topology, matching and design intent. Import is explicit.

Create an operation region around the objects to work on, inspect its interfaces and give its ID to a layout agent. An interface missing physical access can remain a declared obligation; drawing a guard or connection later must satisfy it. A virtual marker does not create metal.

## A typical collaboration

1. A human selects the source design and partitions local work.
2. Each layout agent claims one assigned region. Sibling agents can proceed in parallel; a parent manages only direct child shells and interfaces.
3. Build the interior, using directional PIN mode where appropriate. Complete device access, routing and sampled guard structures.
4. Tighten the operation region with the property-panel action/API, select fixed PIN mode and complete real boundary exits.
5. Run local checks frequently and inspect warnings. Run server DRC when appropriate, review active errors and finish only with valid whole-region evidence from both.
6. The human explicitly writes the reviewed workspace to Virtuoso, then performs the required full-chip checks/LVS and final sign-off.

During agent occupancy, physical human edits are locked for that construction level. Navigation remains available; positioning guides are the limited collaboration exception. Do not terminate another client's session to bypass a lock.

## Metal pour

The top-level Rectangle button opens a secondary menu containing Rectangle and 铺铜 side by side. Pour fills a dragged rectangle on a selected metal layer with a named net intent. Its layer drop-down shares the rectangle tool's LPP presentation/default behavior but lists only writable metal drawing layers. Select direct shapes/vias to connect before opening the panel, if needed. The clearance defaults to the largest configured local spacing rule for that layer, or 0.2 µm when no rule exists, and can be adjusted for the process. The panel's 45° cornering checkbox is off by default. Without targets, all independent islands remain; with targets, disconnected islands are removed. A known conflicting target net, out-of-region bounds or incomplete geometry rejects the whole operation.

Pour avoids every same-layer object not explicitly selected as a target and respects keepouts and blackbox routing policy. In the default mode, non-orthogonal obstacles receive conservative axis-aligned covers, and results are ordinary editable polygons with horizontal/vertical edges only, including concave L and U shapes. Holed regions are orthogonally sliced and merged back into simple polygons without holes. Enabling 45° cornering permits 45°/90° edges; supported 45° diagonal obstacles are avoided using their actual 45° contours, while obstacles at unsupported angles still receive conservative axis-aligned covers. Output slices are likewise merged into simple polygons without holes. Output is capped at 512 polygons per call, with one undo for the whole batch. Grid rounding adds a small clearance guard. This only enforces clearance from explicit obstacles; it does not replace width, density, antenna, electrical/thermal or complete DRC/LVS verification. Run local and server checks after pouring.

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

Do not change device parameters to hide a mismatch or invent a waiver. If a native-source baseline check fails, stop and reconcile the actual source/dependency change before synchronizing.

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
| Ctrl+E | Return to the parent level |
| Ctrl+R | Rotate selected supported objects by 90° |
| Ctrl+Z or U | Undo |
| Ctrl+Y or Ctrl+Shift+Z | Redo |
| Ctrl + wheel | Change the hierarchy peek level |
| Esc | Cancel the current interaction |

Shortcuts are configurable in the UI. Text fields keep text-editing behavior. Panel buttons toggle their corresponding panels; auto-routing is available through View rather than the main toolbar.

---

[简体中文](../zh/usage.md) | **English** · [Project home](../../README.en.md) · [Documentation](index.md)
