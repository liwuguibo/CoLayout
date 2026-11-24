# CoLayout

**简体中文** | [English](README.en.md) · [文档目录](docs/zh/index.md) · [初始化提示词](#初始化提示词) · [版图 Agent 提示词](#版图-agent-提示词)

## 视频介绍与演示

作者录制的三期中文视频（Bilibili），可按顺序观看：

1. [01 · 和 AI 一起画模拟版图：软件介绍与运放示例](https://www.bilibili.com/video/BV1514w6eEzK/)：介绍软件界面和人机协作方式，用运放版图演示布局、连线、检查及同步流程。
2. [02 · 环境搭建、工艺配置与 Virtuoso 层次化设计](https://www.bilibili.com/video/BV1Ly4w6NEEy/)：从项目初始化、连接 Virtuoso 和工艺样例配置开始，演示原生子模块、模块与 PIN 安排、Agent 协作连线及同步，并保留排查和调整过程。
3. [03 · 聊聊 AI 辅助模拟版图工具的设计思路](https://www.bilibili.com/video/BV1334w6ZEYm/)：介绍操作区划分、共享工作区、版图数据保存、Agent API 和 SKILL 通信，以及保护环、DRC、PIN 连通性与工艺适配的设计考虑。

## 项目简介

CoLayout 是面向人和 Agent 协作的轻量化 Windows/Linux 版图工作区，与 Cadence Virtuoso 配合使用。人可划分本地操作区并在 Virtuoso 中自动投影非制造警示边界（提醒区外人类编辑避让），版图 Agent 在操作区内通过本机 HTTP API 工作，人类同时可直接在 Virtuoso 完善 GUI 中编辑操作区外部版图；只要双方物理范围错开，即可随时双向合并与推送。工作区、权限、撤销、检查和显式同步由桌面 APP 统一管理。

软件用于辅助版图工作，不代替完整 LVS 或工艺签核。Virtuoso、Calibre 和 PDK 由用户自行合法取得，本仓库不包含这些产品。

## 复制提示词开始使用

第一次部署使用下面的“初始化提示词”；软件准备好后，在专用客户端文件夹中新开对话，使用“版图 Agent 提示词”。

### 初始化提示词

交给负责安装和后续源码维护的 Agent。填写服务器、SSH 用户名和工艺名称即可。

<!-- BEGIN SETUP PROMPT -->
~~~text
请将 https://github.com/zyc649/CoLayout 克隆到本地，按仓库 AGENTS.md 完成初始化：配置环境和项目级技能，启动 APP 并连接 Virtuoso，建立版图 Agent 文件夹，再根据已打开并保存的 Layout 样例推送工艺配置，供我审核。

服务器：<IP>；SSH 用户：<用户名>；已配置免密登录。
工艺名称：<名称>。样例已按文档准备，没有双排保护环。
~~~
<!-- END SETUP PROMPT -->

[提示词说明](docs/zh/setup-prompt.md) · [样例准备清单](docs/zh/setup.md) · [初始化执行手册](docs/zh/agent-setup.md)

### 版图 Agent 提示词

在初始化时建立的客户端文件夹中新开对话，只授予该文件夹权限。填写操作区 ID 和任务，不需要服务器账号。

<!-- BEGIN LAYOUT PROMPT -->
~~~text
请阅读本文件夹的 AGENTS.md，连接 CoLayout 并查询可用 API，按文档在指定操作区完成版图任务。只通过 APP 操作，不访问服务器或修改软件源码。

操作区 ID：<ID>
任务：<具体要求>
~~~
<!-- END LAYOUT PROMPT -->

[版图提示词说明](docs/zh/layout-prompt.md) · [软件使用说明](docs/zh/usage.md)

## 阅读入口

| 你要做什么 | 从这里开始 |
|---|---|
| 安装、连接服务器、配置工艺 | [准备清单](docs/zh/setup.md) → [Agent 执行手册](docs/zh/agent-setup.md) |
| 与 Agent 一起画版图 | [使用说明](docs/zh/usage.md) → [API 说明](docs/zh/api.md) |
| 接手源码、修 bug 或重构 | [接手指南](docs/zh/maintainer-guide.md) → [架构](docs/zh/architecture.md) → [实现详解与测试索引](docs/zh/internals.md) |
| 查找其他文档 | [完整中文目录](docs/zh/index.md) |

## 两个对话，两种职责

- **初始化／软件维护对话：** 拉取并维护源码，安装依赖，选择服务器和 Virtuoso 窗口，采集工艺样例并提交待审核配置。
- **版图对话：** 单独打开一个对话，只授予生成的版图 Agent 文件夹权限。它只使用 APP API，不直接使用 Bridge、SSH 或软件源码。可以为两种工作选择不同的模型。

## 开始使用

新开对话时可使用[初始化提示词模板](docs/zh/setup-prompt.md)，填写自己的目录、服务器和 SSH 用户名。

准备 Windows 或 Linux 桌面、Git、[uv（官方英文安装说明）](https://docs.astral.sh/uv/getting-started/installation/)，以及可通过 SSH 访问、已安装合法 EDA 软件的 Linux 服务器。初始化 Agent 可以协助安装和诊断，但需要你提供服务器地址及必要的连接信息。

1. 拉取仓库，让初始化 Agent 阅读 [AGENTS.md（供初始化 Agent 阅读，英文）](AGENTS.md)。
2. Windows 执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1`；Linux 执行 `./setup.sh`。
3. 按[安装说明](docs/zh/setup.md)配置、检查服务器连接并启动 APP。地址缺失、SSH 认证失败或存在多个 Virtuoso 窗口时，应先明确处理。
4. 使用项目级 `colayout-pdk` skill 采集已保存的 Layout 样例。Agent 提交提案，人在 APP 中审核并启用。
5. 使用 `colayout-layout-agent` skill 生成独立工作文件夹，再新开版图对话。

仓库自带两项 CoLayout 技能；安装步骤另行下载固定版本的上游 Virtuoso skill，放入项目级技能目录。

## Linux 桌面移植

Linux 版本与 Windows 复用同一个 Qt 工作区、版图模型、Agent API 和显式同步链路，不维护另一套界面或数据语义。桌面适配包括 Linux 启动器、中文字体回退、应用图标与单实例识别、POSIX 客户端文件锁，以及可覆盖取消、超时和正常关闭的进程组管理；Windows 启动与原生窗口集成继续保留。

- 安装：准备 Git、uv、Python 3.13 和 Qt 所需系统库后执行 `./setup.sh`，再用 `.venv/bin/python tools/check_environment.py` 检查环境。
- 启动：执行 `./Start-CoLayout.sh`，或用 `./tools/project.sh start`；`./tools/project.sh status` 查看当前安装状态。重复启动会复用同一安装，不创建第二个工作区写者。
- 客户端：生成的 Linux 客户端使用 `python.sh`，保留独立身份、pending 和归档语义。可选桌面入口由 `tools/install_workspace_shortcut.py` 生成，只写入当前用户目录。
- 验证：CI 同时覆盖 Windows 和 Ubuntu。Linux 的安装、公开回归、真实 HTTP、进程生命周期和桌面验收步骤见仓库根目录的 `LINUX_PORTING_ACCEPTANCE.md`。

公开合成测试不连接服务器，也不等于真实 Virtuoso/Calibre/PDK 原生集成验收；后者仍须使用用户授权的私有测试副本完成。

## 工艺样例需要准备什么

代表性的 MOS、电阻、原生 MIM 电容（工艺支持时），以及每对相邻金属之间独立的默认 1×1 标准孔，包括顶层金属。每种需要的 P/N 保护环，要提供含直段、拐角和两个端头的原生 90° 开放 MPP 路径。双排必须提供真实双排样例，否则不启用。额外可绘制工艺层需提供独立图形，并由人审核勾选。

还需要真实 DRC 主规则、stream map、工具和环境文件。缺失时先补齐，不能拿其他 PDK 的规则替代。详见[完整准备清单](docs/zh/setup.md)。

## 软件的基本使用逻辑

- 兄弟区、父子区可分别接入 Agent；同一区域独占编辑。
- Virtuoso 与 CoLayout 支持解耦空间协同：划定、缩放或解散操作区时，自动向 Virtuoso 同步基于非数据库瞬态高亮集的不可制造警示边界（`[CoLayout AI Region: ...] DO NOT EDIT INSIDE`），不污染 Layout XL 保存状态。人类可在 Virtuoso GUI 中直接编辑操作区外部版图，AI 在操作区内工作；只要物理位置错开，随时点击“读取”/“写入”或调用 Agent `workspace pull`/`sync`/`release_region(sync_to_virtuoso=true)` 即可按**操作区最终快照无历史覆盖**直接合并（区内未改动旧走线与 `inst` 原生 Layout XL 绑定原地保留，仅删改/重铺变动可写连线、标准过孔与保护环，并保留区外人类编辑与运行中兄弟租约）。若 Virtuoso 编辑落入操作区（`VIRTUOSO_EDIT_IN_REGION`）或存在未保存修改（`VIRTUOSO_UNSAVED_CHANGES`），同步会明确报错拦截并保留双方已有工作。
- 父层只管理直接子区外框和接口，不越层编辑子区内部。
- 可以先用方位 PIN 完成主体，再收紧操作区、转固定 PIN、完成真实引出。
- 普通叶 PCell 参与检查；真正原生层次保持黑盒，只开放公开 PIN 和明确设置的局部布线豁免。
- 支持画线后与按需局部 RC 寄生影响评估（`RC 寄生影响` 面板与共享 `inspect_region(kind="parasitics")` 接口，不持续占用后台计算，属早期参考 `signoff: false`）。高频运行本地检查，在适当阶段运行服务器 DRC。完成验收要求当前全区两项检查均通过；本地检查只是受支持的子集，不是完整 LVS。
- 显式保存／导出用于交换文件，内部恢复备份和原生事务记录独立保留。

## 开发与许可

修改源码时，从[源码接手指南](docs/zh/maintainer-guide.md)、[架构](docs/zh/architecture.md)和[源码实现详解](docs/zh/internals.md)开始，再看[开发说明](docs/zh/development.md)、[API](docs/zh/api.md)与[贡献指南](docs/zh/contributing.md)。初始化 Agent 可直接按[执行手册](docs/zh/agent-setup.md)操作。文档同时面向人和协助开发的 Agent。

CoLayout 原创代码使用 MIT 协议；依赖保留各自的许可证，见 [MIT 许可证原文（英文）](LICENSE) 和[第三方声明](docs/zh/licensing.md)。不分发私人版图、工艺文件、凭据或开发对话。

特别感谢 **[Arcadia-1 及贡献者开发的 Virtuoso Bridge Lite](https://github.com/Arcadia-1/virtuoso-bridge-lite)**。CoLayout 通过它连接 Virtuoso。安装固定到明确的上游提交，保留一个有说明的 Windows 兼容补丁，不随上游更新而自动升级。详见[依赖来源](docs/zh/dependencies.md)。

---

**简体中文** | [English](README.en.md) · [文档目录](docs/zh/index.md) · [初始化提示词](#初始化提示词) · [版图 Agent 提示词](#版图-agent-提示词)
