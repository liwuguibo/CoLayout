# Agent API

**简体中文** | [English](../en/api.md) · [项目首页](../../README.md) · [文档目录](index.md)

参数细节见[接口 schema（JSON）](../../templates/layout-agent/docs/api-schema.json)。实际使用前先查询运行中 APP 的接口发现结果。

## 通信与发现

APP 在 127.0.0.1 提供带身份验证的 HTTP 服务，将当前 URL/token 发布到 agent-connection.json。该文件是私有运行数据。使用生成的客户端目录，不写死端口；GET 不是工具调用协议。

向 /call 发送 POST，带 Authorization: Bearer <local-token>，JSON 对象必须恰好包含 tool、arguments、request_id 和 client_id。服务器限制请求大小并拒绝非有限 JSON 数值。建议使用附带客户端处理响应与轮询。

```json
{
  "tool": "workspace",
  "arguments": {"action": "status"},
  "request_id": "caller-generated-unique-id",
  "client_id": "layout-agent-1"
}
```

特殊的 tools 调用返回当前可用工具的 schema，不计入下面的业务工具。默认开放 10 项；人端工艺设置启用算法自动布线后再开放 route_region。静态 schema 列出全部可能工具，运行中的接口发现及权限检查优先。目前不支持 MCP 通信。

## 工具

| 工具 | 用途 |
|---|---|
| workspace | 状态查询、存档保存/加载、操作区边界管理与 Virtuoso 双向空间协同合并 |
| reference_cases | 只读查询人提供的参考案例 |
| process_configuration | 初始化角色的需求查询、提案提交/查询和配置导出；无 API 批准动作 |
| claim_region | 独占一个操作层 |
| inspect_region | 查询本区域的版图、规则、连通、原理图参考以及按需 RC 寄生影响评估 |
| edit_region | 校验后采用编辑批次，包含受支持的边界、PIN、保护环和局部布线豁免 |
| route_region | 默认关闭，仅人在工艺库启用后开放的算法自动布线 |
| check_region | 本地受支持 DRC 与连通检查任务 |
| capture_region | 区域只读截图，不改变人的视角 |
| server_drc_region | 服务器区域检查与结果查询 |
| release_region | 释放/阶段交接，或在证据通过后标记完成 |

API 不开放任意 Python/SKILL、直接 Bridge 访问、任意未绑定源 cellview 导入、PCell 内部编辑或人端工艺批准。

工艺库“绘图与图层”页的“允许 Agent 使用算法自动布线”默认不勾选，旧配置也按关闭处理。该权限只限制 route_region：关闭时不列入实时 tools，generate/recheck/apply 返回 AGENT_AUTOROUTE_DISABLED；已启动任务仍可 status/discard，原请求的幂等回执保留。人类 UI 自动布线以及 Agent 的排列、对齐、自动过孔、多层路径等工具不受影响。inspect_region(kind=rules) 返回 agent_autoroute_enabled；routing 层/孔参数仍可读取，但不代表授权。process_configuration.submit 无权改变权限，省略字段时保留人端选择，尝试改变返回 AGENT_PERMISSION_UI_ONLY。共享配置导入后默认关闭，须由本机人端勾选并审核保存。

edit_region 的 tighten_region/tighten_child_region 与人端“收紧操作区”共用几何逻辑：按内容及直接子区 PIN 外形收紧，各轴自动预留自身/直接子区最大 PIN 尺寸的一半，再加 10%（至少一个制造网格），向外取整。padding_um 为额外叠加余量；返回 pin_clearance_um 为自动余量。仅在原框内收缩，不移动原生几何，不改变 PIN 模式，也不对已经过紧的原框额外提示余量不足。

edit_region 的 create_pour 在矩形 bounds_um 内为一种可写金属 drawing LPP 铺铜，net_intent 和 clearance 必填。targets 只能显式列出本层普通图形/过孔 UID；其他同层金属、禁布区与黑盒策略均按间距避让，已知网络冲突会拒绝。指定目标后删除未连接铜岛，省略目标则保留所有独立铜岛。间距包含制造网格取整保护。

allow_45 可选，默认 false。默认模式下，非正交障碍使用保守轴对齐包围，输出仅含水平/垂直边；正交切片重新合并为无孔简单多边形，支持 L/U 形凹角。allow_45=true 时允许 45°/90° 边，支持的 45° 斜障碍按实际 45° 轮廓避让，非支持角度仍使用保守轴对齐包围；输出切片同样合并为无孔简单多边形。单次最多 512 个多边形。该动作单项异步提交并原子采用，所有 UID 共用一次撤销。几何间距不代替 DRC/LVS，仍须运行本地和服务器检查。

### 寄生参数按需检查 (`inspect_region(kind="parasitics")`)

`inspect_region` 支持 `kind="parasitics"`，与人端 UI 共享同一个按需本地 RC 评估领域服务（`signoff: false`）。参数说明：
- `uids`: 可选，非空对象 UID 字符串列表（`list[str]`）。
- `net`: 可选，字符串网络名。
- `target`: 可选，目标描述（`CaptureTarget`）。
  *(注：`uids`、`net`、`target` 互斥，每次调用最多指定其一)*。
- `bounds_um` / `markup_id`: 可选的裁剪范围或标记 ID（支持 `padding_um`, `clip_to_region`）。
- `analysis_radius_um`: 近邻分析半径（`>0, <=100` µm，默认 `10` µm）。

`inspect_region(kind="rules")` 同时报告 `parasitics` 元数据（包含 `configured`、`mode`、`rc_model_digest`）。
CLI 访问方式：`./python.sh api.py --client <name> inspect parasitics`（支持 `--net CLK_P`、`--uids U1 U2` 或重复 `--uid`、`--radius-um 12`、`--bounds ...`、`--markup-id ...`）。

### Virtuoso 与 CoLayout 空间协同（操作区警示边界与即时合并）

人类可在 Virtuoso GUI 中直接编辑操作区外部版图，AI/CoLayout 则专注于划定的 `OperationRegion` 内部。只要两者物理范围错开，任一方完成后均可立即同步，互不阻塞（包括存在进行中草稿与运行中 Agent 租约时）：

- `workspace` 支持操作区与双向同步动作：
  - `status`：新增返回 `regions` 列表（每项含 `id`、`name`、`parent_id`、`pin_mode`、`bounds_um`）。
  - `create_region`（传入 `bounds_um`、可选 `name`/`pin_mode`/`parent_id`/`blocked_layers`/`note`）：创建操作区并自动向 Virtuoso 写入不可制造警示框；`dissolve_region`（传 `region_id`）：解散操作区并清理对应警示框；`sync_regions`：手动重绘 Virtuoso 中的全部警示边界。
  - `pull`：读取 Virtuoso 区外人类改动并与本地未提交草稿/活跃 Agent 租约三路合并。
  - `sync`：推送全工作区非占用区域的修改，或传入可选 `region_id` 仅推送指定已完成区域子树（若该区域或子区仍被占用返回 `WORK_SESSIONS_ACTIVE`）；其余兄弟区域草稿与租约继续保留。
  - 原始 `/call` 调用 `create_region`/`dissolve_region`/`pull`/`sync` 时须提供 `expected_document_id` 与 `expected_revision`；使用客户端 `api.py workspace ...` 会自动读取 `status` 填充。
- `release_region(outcome="done", sync_to_virtuoso=true)`（CLI：`api.py --client <name> release --outcome done --sync-to-virtuoso`）可在验收通过并释放占用后立即将该操作区改动协同推送到 Virtuoso。
- CLI 示例：
  - `./python.sh api.py --client <name> workspace create_region --bounds 0 0 40 30 --name M1_CORE --pin-mode directional`
  - `./python.sh api.py --client <name> workspace pull` / `./python.sh api.py --client <name> workspace sync [--region-id <id>]` / `sync_regions` / `dissolve_region --region-id <id>`
- **操作区越界保护（`VIRTUOSO_EDIT_IN_REGION`）**：无论执行 `pull` 还是 `sync`，只要检测到 Virtuoso 中存在新增、移动/修改或删除且与任一操作区物理重叠的元素，同步立即中止，并在错误信息中列出具体越界元素的名称、类型、图层、微米坐标与所属操作区，提示人类在 Virtuoso 中删除或移出后再重试，双方既有工作均完整保留。
- **未保存保护（`VIRTUOSO_UNSAVED_CHANGES`）**：若 Virtuoso 当前 cellview 有尚未按 `F2` 保存的内存修改，会明确报错拦截，避免静默覆盖。

## 身份、版本和任务

先 claim，保留返回的 session token；每个并行执行者使用独立客户端身份。编辑前 inspect，并提交 expected_revision 和实际对象 ID。不能以全局 revision 为由覆盖本区域的过期输入。

请求由 client_id + request_id + 相同 body 确定。网络超时后保留这组值，换 ID 可能产生第二次操作。对账成功前保留 pending。queued/running 不表示成功，应轮询原 job_id。遇到 stale/cancelled/failed，先理解结果、重新查询，再调整操作。

服务器 DRC 不强制先取得本地通过；done 仍要求当前全区本地和服务器两类证据。建议性 warning 与阻断错误分开。人端批准的报告过滤统一用于 UI、API 和服务器验收，原始已过滤记录仍可查看；它不能豁免本地物理错误。

## 客户端生成与 schema 维护

tools/create_layout_agent.py 仅复制执行模板、说明及生成的 SDK/schema，不复制另一个 Agent 的任务、身份或 pending。更新已有托管文件前核对哈希。目录权限由外部 Agent 宿主执行，说明文件不是安全沙箱。

修改 agent_tools/contract.py 后运行（Linux 将解释器替换为 `.venv/bin/python`）：

```powershell
.\.venv\Scripts\python.exe -m tools.build_customer_api_kit
.\.venv\Scripts\python.exe -m tools.build_customer_api_kit --check
```

再用 --update 更新指定的空闲客户端目录。不能默默扩大版图 Agent 到维护或工艺配置权限。

## 实现归属

http_api 把调用交给所属线程；session/leases 处理占用和重放；region_edits/region_tasks 编排领域服务和不可变后台输入；inspection/capture 保持范围；release_status 核对证据。业务规则放在共享 Editor、区域、物理和工艺服务，不在 HTTP 分支重复实现。

---

**简体中文** | [English](../en/api.md) · [项目首页](../../README.md) · [文档目录](index.md)
