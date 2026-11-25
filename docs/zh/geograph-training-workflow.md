# Geo-Graph RC 代理模型训练工作流

**简体中文** | [English](../en/geograph-training-workflow.md) · [项目首页](../../README.md) · [文档目录](index.md)

这份脱敏参考工作流说明如何从合法授权的 foundry/PDK 层叠定义和 golden PEX 提取结果，训练可在 CPU 上运行的寄生 RC 代理模型（**Geo-Graph Residual MLP**）。

> **分发边界**：仓库不包含 foundry PDK 文件（`.ircx`、`.proc`、`.layermap`、LVS/PEX 规则 deck）、生成的版图、提取网表、训练数据集或训练权重。所有 `<PLACEHOLDER>` 都应替换为自己的授权路径和 EDA 服务器配置。工艺衍生数据应保持私有，每次导出分享前都要审核。
>
> **外部工具**：下文的数据集生成、远程 PEX、训练和盲测脚本是在独立训练工作区中的参考入口，目前不随 CoLayout 分发。CoLayout 自带 `colayout_lite/parasitic_nn/` 下的几何、连接、解析电阻和纯 NumPy 推理模块。

## 流程概览

```text
[远程 EDA 服务器]                [本地工作站]                         [云端 GPU（如 Colab）]
1. 提取 PDK 层叠 ─────────▶ 2A. 生成拼块 GDS/CDL ─────────▶ 2B. 批量远程 LVS + PEX
   (<PDK_ROOT>/)               (TiledGDSGenerator)              (<PEX_TOOL> 执行器)
                                                                          │
4. 盲测与推理 ◀──────────── 3. GPU 超图训练 ◀────────────── 2C. 解析 .pxi 并建图
   (GeoGraphRCEngine)          (PyTorch -> NumPy .npz)          (LayoutGraphExtractor)
```

| 阶段 | 环境 | 关键模块或入口 | 输入 | 输出 |
|---|---|---|---|---|
| **1. PDK 解析** | 远程 SSH + 本地 | `pdk_openems.py`、`colayout_lite/parasitic_nn/geometry_graph.py`、`via_connectivity.py` | `<PDK_ROOT>/` 技术文件和 layermap | 私有层叠表、GDS 层映射和过孔规格 |
| **2A. 拼块 GDS 生成** | 本地训练工作区 | `generate_tiled_gds.py`、`build_training_dataset.py` 阶段 `a` | 随机种子、网格和过孔比例 | `<prefix>.gds`、`<prefix>.cdl`、`<prefix>_meta.pkl` |
| **2B. 远程 PEX** | 授权的远程 EDA 服务器 | `remote_xrc_runner.py`（`run_remote_xrc_batch`） | 本地 `.gds`/`.cdl`、`<PDK_RCX_DECK>` | 合并的 `.pxi` 寄生网表 |
| **2C. 图数据集** | 本地训练工作区 | `build_training_dataset.py` 阶段 `c` | `<prefix>_meta.pkl`、`<prefix>.pxi` | `weights/rc_dataset.json` |
| **3. 模型训练** | 云端 GPU | `train_colab_geograph.py` | `rc_dataset.json`（可选用 `.gz`） | 纯 NumPy 权重 `geograph_rc.npz` |
| **4. 推理与评估** | 本地 CPU + 授权远程核对 | `colayout_lite/parasitic_nn/model.py`、`verify_blind_gds_vs_pex.py` | `RectangleElement` 列表和 golden PEX | RC 估算、精度和延迟测量 |

## 阶段 1：PDK 层叠提取与本地建模

1. 从合法授权的 PDK 目录（`<USER>@<EDA_SERVER>:<PDK_ROOT>/`）提取工艺文件到本地已被 Git 忽略的目录，例如 `pdk_private/`：
   - 互连技术文件，包括 `.ircx`、`.proc` 和 RC 参数表。
   - GDSII/OASIS 层映射文件（`.layermap`）。
   - LVS/PEX 提取规则 deck（`<PDK_RCX_DECK>`）。
2. 在训练工作区配置私有层叠与层映射：
   - 参照 `colayout_lite/parasitic_nn/geometry_graph.py` 的结构，填写 `LAYER_STACK_SPEC`（$z_{\min}$、$z_{\max}$、厚度和 $R_{\text{sh}}$）、`GDS_DRAWING_MAP`、`GDS_PIN_TEXT_MAP` 和有效介电常数。
   - 参照 `colayout_lite/parasitic_nn/via_connectivity.py`，填写金属间过孔定义（`VIA_STACK_SPEC` 和 `GDS_VIA_MAP`）。
   - 可选：在外部 `pdk_openems.py` 中配置 3D FDTD 层叠，用于全波电磁交叉核对。
3. 不要用未经审核的训练常量替换 CoLayout 已审核的工艺配置。内置合成层叠只用于测试，不代表任何 foundry 工艺。

## 阶段 2：参数化版图与远程 PEX 批处理

外部数据集流程由 `build_training_dataset.py` 编排，可完整执行或分阶段执行：

```bash
python -m build_training_dataset all  # A -> B -> C
python -m build_training_dataset a   # 本地拼块 GDS/CDL 生成
python -m build_training_dataset b   # 授权的远程 PEX 提取
python -m build_training_dataset c   # 本地 .pxi 解析与图组装
```

### 步骤 A：拼块多场景 GDSII 与 CDL 生成

- 参考生成器为 `generate_tiled_gds.py` 中的 `TiledGDSGenerator.build_batch_gds()`。
- 将 $N_{\text{rows}} \times N_{\text{cols}}$ 个独立 `20 um x 20 um` 拼块放入 `lvs_top.gds` 和 `lvs_top.cdl`，边缘间隔为 `40 um`（节距 `60 um`），以分摊 EDA 工具启动开销。
- 场景包括单走线、耦合总线、跨层交叉、L 弯、修改前后样本对、跨层跳线、叠孔、过孔阵列以及先画线后落孔样本对。
- 使用 `resolve_layout_connectivity()` 解析 3D 连接并分配满足 LVS 的端口标签；外部生成器写出 `<prefix>_meta.pkl`。

### 步骤 B：远程 LVS 与 PEX 执行

- 参考入口为 `remote_xrc_runner.py` 中的 `run_remote_xrc_batch()`。
- 仅在授权的私有训练工作区配置 `PEX_BIN`、`ORIG_RCX_DECK`、`REMOTE_ROOT`（`<REMOTE_WORKDIR>`）和 `remote_host`（`<USER>@<EDA_SERVER>`）。
- 典型批次流程：
  1. 通过 SSH/SCP 将 `lvs_top.gds` 和 `lvs_top.cdl` 上传到 `<REMOTE_WORKDIR>/<batch_name>/`。
  2. 将 `<PDK_RCX_DECK>` 复制为 `block.rcx`，并设置提取温度。
  3. 按授权工具文档使用层次化和多核选项执行 LVS 与 RCC 提取。
  4. 将地电容和耦合电容网表（`net.dist` 和 `*.pxi`）取回私有本地数据集目录。
- 不输出凭据、不发布专有规则 deck，也不对生产设计执行未经批准的写入。

### 步骤 C：golden 标签解析与 $E(2)$ 不变图组装

1. 从 `.pxi` 解析 SPICE 电容元件，得到每网络 $C_{\text{tot}}$、$C_{\text{gnd}}$ 和成对 $C_{\text{cpl}}$，单位为 fF。
2. 每个拼块提取五层垂直滑动窗口 `[L-2, L+2]` 和水平 halo（`r_halo_um=12.0`），使用 `LayoutGraphExtractor` 计算满足平移、90° 旋转和镜像不变性的节点及边特征。
3. 在外部训练工作区将私有图数据集写入 `weights/rc_dataset.json`。

## 阶段 3：GPU 训练与纯 NumPy 导出

1. 在 Google Colab 或其他授权 CUDA 环境中使用外部 `train_colab_geograph.py`。
2. `train_on_batched_dataset()` 将训练图打包为一个不连通的超图，使每轮只执行一次向量化 GPU 前向/反向传播。参考目标联合监督 $C_{\text{tot}}$、$C_{\text{gnd}}$、成对 $C_{\text{cpl}}$、增量 $\Delta C_{\text{tot}}$ 和落孔并网 $\Delta C_{\text{merge}}$。
3. 使用 `GeoGraphResidualNet.export_numpy_weights()` 导出最佳验证检查点。归档包含特征归一化统计和各层权重，使推理不再依赖 PyTorch。
4. 将生成的 `.npz` 放在本地 Git 忽略的权重目录。记录其 SHA-256，并在审核 CoLayout RC 模型时配置该摘要；不要提交权重。

## 阶段 4：盲测与本地推理

### 4.1 对照 golden PEX 盲测

使用外部 `verify_blind_gds_vs_pex.py` 生成未参与训练的拼块，执行授权的远程 PEX，并测量 $C_{\text{tot}}$ MAPE/MAE、增量 $\Delta C_{\text{tot}}$ MAE 和 CPU 延迟。不要用盲测集调参。CoLayout 的 `AnalyticalResistorEngine` 负责计算平面走线和过孔栈的串联电阻。

### 4.2 本地 Python 推理 API

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

实际延迟和精度取决于训练权重、几何、硬件与验证集。CoLayout 的交互 RC 结果仍是 `signoff: false` 的早期设计估算；此工作流不替代 DRC、LVS、foundry 认可的提取或签核。

---

**简体中文** | [English](../en/geograph-training-workflow.md) · [项目首页](../../README.md) · [文档目录](index.md)
