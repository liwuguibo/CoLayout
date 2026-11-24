# CoLayout 改动说明

## beta：Virtuoso 与 CoLayout 解耦双向空间协同

在 `alpha` 分支基础上将人类 Virtuoso GUI 编辑与 CoLayout AI 操作区编辑彻底解耦，只要两者编辑的物理位置错开即可随时互相同步：

- **Virtuoso 原生操作区警示边界**（[cooperative.il](colayout_lite/skill/cooperative.il)、[cooperative_sync.py](colayout_lite/cooperative_sync.py)）：创建、改尺寸/收紧、加载或解散操作区时，自动通过 `ltSyncRegionGuides` 在 Virtuoso 对应窗口以非数据库瞬态高亮集（`geCreateHilightSet` / `geAddHilightRectangle` / `geAddHilightLabel`）绘制不可制造警示框与 ASCII 标签 `[CoLayout AI Region: <name/id>] DO NOT EDIT INSIDE` 并触发 `ltRedrawCellView`，**不改写 OA 数据库图形、不递增 `modifiedCounter`，也不会触发 Layout XL 提取而残留 `modifiedButNotSaved=t`**；旧版本遗留的持久化 `colayoutRegionGuide` DB 图形仍会在快照导出（`skill/read.il`、`skill/compact.il`）中过滤并在保存/克隆/服务器 DRC 前通过 `ltClearRegionGuides` 自动清理。
- **操作区越界精准拦截（`VIRTUOSO_EDIT_IN_REGION`）与未保存保护（`VIRTUOSO_UNSAVED_CHANGES`）**：协同 `pull`、`sync`、`guard_preview` 与 `drc/remote.py` 均先核对 Virtuoso `modifiedButNotSaved`，并在遇到走线切分/复制引起的 `DUPLICATE_ROUTE_ID` / `DUPLICATE_FIGGROUP_ID` 时自动触发 `ltPrepare` 去重修复；再通过 `analyze_virtuoso_delta`（校验 `route.members` 及增删改几何）逐项比对基线差异。一旦发现 Virtuoso 中新增、修改/移动、删除元素或路线变化落入任何操作区，立即中止并详细列出越界元素的名称、类型、图层、微米包围盒与所属操作区，提示人类删除或移出操作区后重试，双方已有编辑均完整保留。
- **连续 `create_guard` 与区外并发修复**：`Adapter.sync_region_guides` / `commit` 明确跳过 `COLAYOUT_MVP_LITE_*` 临时预览/验证副本，避免刷新临时副本计数引发连续第二次 `create_guard` 的 `ltRunRequest: SOURCE_CONFLICT`；同时 `Adapter.guard_preview` 支持在区外有已保存人类改动时自动协同变基（仍保留活跃 Agent 租约基线）。
- **区外人类修改随时无损拉取（`pull` / `读取`）**：`Adapter.cooperative_pull` 在确认未越界后，对 Virtuoso 中新建或复制的无持久化 UID 图形自动执行 `ltPrepare` 补齐身份，再通过 `rebase_workspace_onto_virtuoso`（基于最终快照 `overlay_local_snapshot` / `synthesize_operations_from_target`，不再依赖历史 `op.before` 摘要）与 `WorkSessions.validate_cooperative_baseline` 将区外改动合入当前基准，同时完整保留 CoLayout 操作区最终修改状态与未受影响的操作区 Agent 租约（`WorkSession`）。（仅“文件 → 从 Virtuoso 重新导入…”与 `workspace load` 仍作整工作区替换。）
- **基于操作区最终快照的无历史覆盖同步（方案 B，`sync` / `写入` / `release_region(sync_to_virtuoso=True)`）**：将已完成或非占用操作区同步到 Virtuoso 时，不再重放 `operations` 中间编辑历史或校验 `before` / `mpp_before` 快照，而是经 `build_snapshot_overwrite_commands` 按 `colayoutObjectId` 直接对比本地最终审查态与远端当前态：**区内未改动对象原地保留**（不破坏已有 `oaRoute` 与原生属性）、**`inst` / 已有 MPP 仅原地更新位姿**（保留 Layout XL Binder 绑定）、**远端多余可写图形/保护环直接删除**、**修改或新增的可写连线/标准过孔/保护环直接通过 SKILL `ltReplaceShape` / `ltCreate` / `ltGuard` 覆盖写入**（保留非 `figGroup` 约束与原 `oaRoute` 成员槽位），再保存（`dbSave`）、重绘画布并完成读回核对，同时在 CoLayout 本地继续保留仍在运行中的兄弟操作区草稿与租约。
- **Agent API 与 CLI 扩展**：`workspace` 新增 `create_region`、`dissolve_region`、`sync_regions`、`pull` 与 `sync`（可选 `--region-id`）动作，`workspace status` 返回 `regions` 摘要；`release_region` 新增 `sync_to_virtuoso` 参数（CLI 为 `--sync-to-virtuoso`），支持区域 `done` 释放后立即推送到 Virtuoso。

## alpha：按需 RC 寄生评估、M1 长距离分区与铺铜

在 `linux` 分支基础上加入单层矩形铺铜、交互式画线/图层/标尺增强、Geo-Graph v3 按需 RC 寄生影响评估与 M1 长距离分区内核，并强化 IC618 SSH 隧道恢复与提取：

### 按需 RC 寄生与 M1 长距离分区（`alpha` 后续更新）

- **Geo-Graph v3 纯 NumPy 神经网络代理与交互画线预览**：加入 `parasitic_nn`（含 3D 过孔聚类连接与节点电导求解）、`wire_capture.py` / `wire_panel.py` 正交 L 形/换层过孔实时预览草稿，以及 `layer_palette.py` 图层调色板、`ruler_tool.py` 交互标尺、底栏实时微米读数、锁线切换与“复制任务上下文”。
- **按需评估生命周期与共享人机接口**：交互编辑、拖框和自动布线均不执行持续后台计算；用户可在画线提交后点击“查看本次走线 RC”或在“RC 寄生影响”面板点击“计算当前 RC”按需对比 `ΔR / ΔCg / ΔCc` 并高亮拓扑，后续编辑将已有结果标记为过期；Agent 通过共享服务 `inspect_region(kind="parasitics")`（CLI：`api.py inspect parasitics`）获取相同评估结果（`signoff: false`）。
- **M1 Core-Halo 长距离分区内核**（[parasitic_partition.py](colayout_lite/parasitic_nn/parasitic_partition.py)、[geometry_graph.py](colayout_lite/parasitic_nn/geometry_graph.py)）：提供 `build_core_halo_partitions` 与 `evaluate_partitioned_capacitance`，支持 `auto` / `none` / `path_tiles` / `subregions` / `spatial_grid` 分区、拼缝虚拟边界边抑制（`_is_artificial_seam_edge`）、`ViaCluster` 原子归属、零重复计数的 `Cg`/`Cc` 守恒及各分区占比与瓶颈报告。
- **IC618 连接与提取加固**：完善 SSH 隧道安全恢复及 IC618 原理图/版图对应检查（保留 `INFO (LX-1001)` 与已确认 `lxDummy` 器件处理）。

### 铺铜功能

在 `linux` 分支基础上加入单层矩形铺铜，人端界面与 Agent API 共用同一领域实现。

- 工具栏新增“铺铜”：选择金属层、目标网络和最小间距后，在画布拖出铺铜范围；45° 拐弯复选框默认关闭。
- 可显式选择当前层普通图形或过孔作为连接目标；未指定的同层金属一律作为障碍。
- 指定目标后删除未连接铜岛；未指定目标时保留所有独立铜岛。
- 遵守操作区边界、禁布区和原生黑盒策略，拒绝已知网络冲突、缺失几何和不安全结果。
- 间距不低于该层已配置的本地 spacing 规则，并增加制造网格取整保护。
- 默认仅生成水平/垂直边，非正交障碍使用保守轴对齐包围；正交切片合并为无孔简单多边形，支持 L 形、U 形等凹多边形，单次最多 512 个。
- 可选 45° 拐弯允许 45°/90° 边；支持的 45° 斜障碍按实际 45° 轮廓避让，非支持角度仍使用保守轴对齐包围，输出切片同样合并为无孔简单多边形。Agent 对应参数为 `create_pour` 的 `allow_45`，默认 `false`。
- 整批铺铜共享一次撤销。Agent 通过 `edit_region` 的 `create_pour` 单项异步任务原子采用。

铺铜只保证显式障碍间距，不替代最小宽度、密度、天线、电热、完整 DRC 或 LVS。生成后仍需运行本地与服务器检查；尚未在真实 PDK/Virtuoso 上验证原生同步。

## linux：Linux 桌面移植

- 保留 Windows 支持，Linux 与 Windows 复用同一个 Qt 工作区、版图模型、Agent API 和显式同步链路。
- 新增 `setup.sh`、`Start-CoLayout.sh`、`tools/project.sh` 和 Linux 客户端 `python.sh`。
- 加入 Linux 中文字体回退、应用图标与单实例识别、POSIX 客户端锁、用户级桌面入口和进程组生命周期管理。
- CI 同时覆盖 Windows 和 Ubuntu；主要 README 增加安装、启动、客户端和验收说明。
- 根目录 `LINUX_PORTING_ACCEPTANCE.md` 区分合成功能验收与真实 Virtuoso/Calibre/PDK 原生集成验收。
- 保留并验证此前的工作区、原生读取和导入修正，不将合成测试表述为真实工艺签核。

## 验证记录

- `linux`：643 项公开测试通过，1 项跳过。
- `alpha`：663 项公开测试通过（铺铜阶段），随后更新至 719 项公开测试通过、1 项跳过（含 Geo-Graph v3 按需 RC 评估、画线草稿、M1 Core-Halo 分区内核、Agent 接口一致性与 SSH/IC618 回归）。
- `beta`：731 项公开测试通过，1 项跳过（涵盖非 DB 高亮集警示边界、`NativeRoute.members` 区外走线合并/切分去重、`VIRTUOSO_EDIT_IN_REGION` 越界拦截、`VIRTUOSO_UNSAVED_CHANGES` 检查、活跃租约保留、局部区域推送、无同步连续 `create_guard` 以及方案 B 操作区最终快照无历史覆盖同步回归），另通过已授权私有测试副本完成端到端真实 Virtuoso IC618 协同验证（含未触碰 `oaRoute` 与 Layout XL 绑定保留、`ltReplaceShape` 覆盖重写与区外人类编辑无损并存）。
- 三个分支均通过 Ruff、客户端 schema、文档链接和发布/隐私审计。
- Linux 固定 Bridge 依赖来源、归档校验和许可证检查通过。
- `alpha` 的真实显示服务器 Qt 拖框与 45° 控件测试通过。
- 早期 `alpha` 使用 `uv 0.13.7`、官方 PyPI 和固定 Bridge 归档完成独立干净安装；此前的 TLS 失败在重试后恢复，未切换镜像。`check_environment.py`、663 项公开回归、`smoke_onboarding` 与 `smoke_layout_client` 均在该环境通过。smoke 使用合成几何和真实 APP/HTTP，不包含 SSH、服务器 DRC 或真实 PDK。
