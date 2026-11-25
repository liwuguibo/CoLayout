# Geo-Graph RC surrogate training workflow

[简体中文](../zh/geograph-training-workflow.md) | **English** · [Project home](../../README.en.md) · [Documentation](index.md)

This sanitized reference workflow describes how to build a CPU-friendly parasitic RC surrogate model (**Geo-Graph Residual MLP**) from licensed foundry/PDK stackup definitions and golden PEX extraction results.

> **Distribution boundary**: Foundry PDK files (`.ircx`, `.proc`, `.layermap`, LVS/PEX rule decks), generated layouts, extracted netlists, training datasets and trained weights are not included in this repository. Replace every `<PLACEHOLDER>` with your own licensed paths and EDA server configuration. Keep process-derived assets private and review every export before sharing it.
>
> **External tools**: The dataset-generation, remote-PEX, training and blind-verification scripts named below are reference entry points in a separate training workspace; they are not currently shipped with CoLayout. CoLayout includes the geometry, connectivity, analytical-resistance and pure-NumPy inference modules under `colayout_lite/parasitic_nn/`.

## Pipeline overview

```text
[Remote EDA Server]               [Local Workstation]                 [Cloud GPU (e.g., Colab)]
1. Extract PDK Stackup ───────▶ 2A. Tiled GDS/CDL Generation ──────▶ 2B. Remote LVS + PEX Batch
   (<PDK_ROOT>/)                    (TiledGDSGenerator)                  (<PEX_TOOL> runner)
                                                                               │
4. Blind Verify & Inference ◀── 3. GPU Super-Graph Training ◀──────── 2C. Parse .pxi & Build Graph
   (GeoGraphRCEngine)              (PyTorch -> NumPy .npz)               (LayoutGraphExtractor)
```

| Stage | Environment | Key module or entry point | Inputs | Outputs |
|---|---|---|---|---|
| **1. PDK parsing** | Remote SSH + local | `pdk_openems.py`, `colayout_lite/parasitic_nn/geometry_graph.py`, `via_connectivity.py` | `<PDK_ROOT>/` techfiles and layermap | Private stackup tables, GDS layer maps and via specs |
| **2A. Tiled GDS generation** | Local training workspace | `generate_tiled_gds.py`, `build_training_dataset.py` stage `a` | Seed, grid size and via ratio | `<prefix>.gds`, `<prefix>.cdl`, `<prefix>_meta.pkl` |
| **2B. Remote PEX** | Authorized remote EDA server | `remote_xrc_runner.py` (`run_remote_xrc_batch`) | Local `.gds`/`.cdl`, `<PDK_RCX_DECK>` | Merged `.pxi` parasitic netlist |
| **2C. Graph dataset** | Local training workspace | `build_training_dataset.py` stage `c` | `<prefix>_meta.pkl`, `<prefix>.pxi` | `weights/rc_dataset.json` |
| **3. Model training** | Cloud GPU | `train_colab_geograph.py` | `rc_dataset.json` (optionally `.gz`) | Pure-NumPy weights `geograph_rc.npz` |
| **4. Inference and evaluation** | Local CPU + authorized remote check | `colayout_lite/parasitic_nn/model.py`, `verify_blind_gds_vs_pex.py` | `RectangleElement` list and golden PEX | RC estimates, accuracy and latency measurements |

## File roles and handoffs

### External training workspace

| File | Purpose and handoff |
|---|---|
| `generate_tiled_gds.py` | Generates reproducible planar, via, perturbation-pair and dense-interconnect scenarios with `TiledGDSGenerator`. Resolves 3D connectivity before assigning pin labels, writes binary GDSII plus a CDL port list, and returns per-tile geometry/scenario/pair metadata for persistence as `_meta.pkl`. It does not run PEX. |
| `build_training_dataset.py` | Orchestrates stages A/B/C. Stage A calls the generator and saves metadata; stage B calls the remote runner; stage C parses golden capacitances, selects each tile's vertical window, extracts graph features, and joins geometry with `gold_c_tot`, `gold_c_gnd`, `gold_c_cpl` and paired-tile IDs in JSON. |
| `remote_xrc_runner.py` | Contains the authorized SSH/SCP and Calibre LVS/xRC batch boundary, returning combined `net.dist`/`.pxi` text. Its parser also normalizes SPICE units and fractured node names into fF-valued total, ground and pairwise coupling labels. Server, executable and deck settings must remain private configuration, not published literals. |
| `train_colab_geograph.py` | Defines the PyTorch `GeoGraphResidualNet` corresponding to the NumPy inference engine: node/edge encoders, two degree-normalized residual message-passing blocks and bounded physics-aware readouts. Builds training/validation super-graphs, optimizes absolute and incremental capacitance losses, selects the best validation checkpoint, and exports `arch_version=3` NumPy tensors plus normalization statistics. |
| `verify_blind_gds_vs_pex.py` | Generates a separately seeded blind set, optionally runs authorized remote PEX, and evaluates the exported weights without training on those tiles. Reports per-net and incremental capacitance errors, via-drop net-merging behavior and CPU latency, with per-tile JSON details. |
| `pdk_openems.py` | Reserved for an optional openEMS/FDTD cross-check of the private stackup. It is not part of the supplied CoLayout workflow and is not needed for PEX-labelled surrogate training. |

### CoLayout modules

| File | Purpose |
|---|---|
| `colayout_lite/parasitic_nn/geometry_graph.py` | Defines rectangle/via geometry and stackup descriptors, local spatial-window selection, graph construction and $E(2)$-invariant features. |
| `colayout_lite/parasitic_nn/via_connectivity.py` | Resolves planar/via connectivity with Union-Find, merges physically bridged nets and forms via clusters for graph features and 3D resistance evaluation. |
| `colayout_lite/parasitic_nn/model.py` | Loads and SHA-256-checks NumPy weights, performs whole-window and before/after incremental inference, and reports net/element capacitance and resistance results. |
| `colayout_lite/parasitic_nn/analytical_resistance.py` | Computes temperature-aware planar and via resistance through the resolved 3D network; this is analytical, not a learned PEX substitute. |
| `colayout_lite/parasitic_nn/parasitic_partition.py` | Provides optional Core-Halo partitioning for long interconnect evaluation, including seam handling and zero-double-count capacitance attribution. |

The handoff contract is the graph record schema and NumPy weight keys, not private PDK paths. When changing either side, verify architecture version, feature dimensions, tensor keys/shapes and normalization statistics together.

## Stage 1: PDK stackup extraction and local modeling

1. Extract process files from your licensed PDK directory (`<USER>@<EDA_SERVER>:<PDK_ROOT>/`) into a local Git-ignored directory such as `pdk_private/`:
   - Interconnect technology files, including `.ircx`, `.proc` and RC specification tables.
   - The GDSII/OASIS layer-mapping file (`.layermap`).
   - The LVS/PEX extraction rule deck (`<PDK_RCX_DECK>`).
2. Configure the private stackup and layer maps in the training workspace:
   - Populate `LAYER_STACK_SPEC` ($z_{\min}$, $z_{\max}$, thickness and $R_{\text{sh}}$), `GDS_DRAWING_MAP`, `GDS_PIN_TEXT_MAP` and effective dielectric permittivities using the structures in `colayout_lite/parasitic_nn/geometry_graph.py`.
   - Populate inter-metal via definitions (`VIA_STACK_SPEC` and `GDS_VIA_MAP`) using `colayout_lite/parasitic_nn/via_connectivity.py`.
   - Optionally configure 3D FDTD stackup definitions in the external `pdk_openems.py` for full-wave EM cross-verification.
3. Do not replace CoLayout's reviewed process configuration with unreviewed training constants. The built-in synthetic stackup is only for tests and is not a foundry process.

## Stage 2: Parametric layouts and remote PEX batches

The external dataset pipeline is orchestrated by `build_training_dataset.py` and can run end-to-end or stage by stage:

```bash
python -m build_training_dataset all  # A -> B -> C
python -m build_training_dataset a   # Local tiled GDS/CDL generation
python -m build_training_dataset b   # Authorized remote PEX extraction
python -m build_training_dataset c   # Local .pxi parsing and graph assembly
```

### Step A: Tiled multi-scenario GDSII and CDL generation

- The reference generator is `TiledGDSGenerator.build_batch_gds()` in `generate_tiled_gds.py`.
- It packs $N_{\text{rows}} \times N_{\text{cols}}$ independent `20 um x 20 um` tiles into `lvs_top.gds` and `lvs_top.cdl`, with `40 um` edge-to-edge isolation (`60 um` pitch) to amortize EDA startup overhead.
- Scenarios cover single traces, coupled buses, cross-layer crossovers, L-bends, before/after perturbation pairs, cross-layer jumpers, stacked vias, via arrays, and trace-first/via-drop pairs.
- `resolve_layout_connectivity()` resolves 3D connectivity and assigns LVS-clean port labels; the external generator writes `<prefix>_meta.pkl`.

### Step B: Remote LVS and PEX execution

- The reference entry point is `run_remote_xrc_batch()` in `remote_xrc_runner.py`.
- Configure `PEX_BIN`, `ORIG_RCX_DECK`, `REMOTE_ROOT` (`<REMOTE_WORKDIR>`) and `remote_host` (`<USER>@<EDA_SERVER>`) only in the authorized private training workspace.
- A typical batch:
  1. Uploads `lvs_top.gds` and `lvs_top.cdl` to `<REMOTE_WORKDIR>/<batch_name>/` through SSH/SCP.
  2. Copies `<PDK_RCX_DECK>` to `block.rcx` and sets the extraction temperature.
  3. Runs LVS and RCC extraction using the licensed tool's documented hierarchical and multi-core options.
  4. Retrieves the ground and coupling netlists (`net.dist` and `*.pxi`) into the private local dataset directory.
- Never print credentials, publish proprietary rule decks, or run an unapproved write against a production design.

### Step C: Golden-label parsing and $E(2)$-invariant graph assembly

1. Parse SPICE capacitance elements from `.pxi` into per-net $C_{\text{tot}}$, $C_{\text{gnd}}$ and pairwise $C_{\text{cpl}}$ in fF.
2. Extract a five-layer vertical sliding window `[L-2, L+2]` and horizontal halo (`r_halo_um=12.0`) per tile. Compute translation-, 90-degree-rotation- and mirror-invariant node and edge features with `LayoutGraphExtractor`.
3. Write the private graph dataset to `weights/rc_dataset.json` in the external training workspace.

## Stage 3: GPU training and pure-NumPy export

1. Use the external `train_colab_geograph.py` in Google Colab or another authorized CUDA environment.
2. `train_on_batched_dataset()` packs the training graphs into one disconnected super-graph, allowing one vectorized GPU forward/backward pass per epoch. The reference objective jointly supervises $C_{\text{tot}}$, $C_{\text{gnd}}$, pairwise $C_{\text{cpl}}$, incremental $\Delta C_{\text{tot}}$ and via-drop net-merging $\Delta C_{\text{merge}}$.
3. Export the best validation checkpoint with `GeoGraphResidualNet.export_numpy_weights()`. The archive contains feature-normalization statistics and layer weights for NumPy inference without PyTorch.
4. Keep the resulting `.npz` file in a Git-ignored local weights directory. Record its SHA-256 and configure the reviewed CoLayout RC model with that digest; do not commit the weights.

## Stage 4: Blind verification and local inference

### 4.1 Blind verification against golden PEX

Use the external `verify_blind_gds_vs_pex.py` to generate unseen tiles, run authorized remote PEX, and measure $C_{\text{tot}}$ MAPE/MAE, incremental $\Delta C_{\text{tot}}$ MAE and CPU latency. Do not tune on the blind set. CoLayout's `AnalyticalResistorEngine` evaluates series resistance across planar traces and via stacks.

### 4.2 Local Python inference API

```python
from colayout_lite.parasitic_nn import GeoGraphRCEngine, RectangleElement

engine = GeoGraphRCEngine(
    weights_path="parasitic_nn/weights/geograph_rc.npz",
    r_halo_um=12.0,
    layer_window=2,
)

existing = [
    RectangleElement(id=1, net_name="VSS", layer="M1", x0=0.0, y0=0.0, x1=20.0, y1=20.0),
    RectangleElement(id=2, net_name="NET_A", layer="M4", x0=2.0, y0=9.5, x1=18.0, y1=10.0),
]
new_shape = RectangleElement(
    id=3, net_name="NET_NEW", layer="M4", x0=2.0, y0=8.8, x1=18.0, y1=9.3
)

result = engine.predict_incremental_rc(existing, new_shape)
print(f"Latency: {result['elapsed_ms']:.3f} ms | Net: {result['self_net']['net_name']}")
```

The actual latency and accuracy depend on the trained weights, geometry, hardware and validation set. CoLayout's interactive RC results remain early design estimates with `signoff: false`; this workflow does not replace DRC, LVS, foundry-qualified extraction or signoff.

---

[简体中文](../zh/geograph-training-workflow.md) | **English** · [Project home](../../README.en.md) · [Documentation](index.md)
