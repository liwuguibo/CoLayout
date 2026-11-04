# CoLayout Linux 移植验收清单

本清单用于验证 **Windows/Linux 双平台适配后的 Linux 桌面版本**。目标是确认相同 Qt 界面、现有功能和数据语义未被改变，不是重新设计或验收新的版图功能。

## 1. 验收结论分级

验收报告必须区分以下两级，不能混写：

- **Linux 合成功能验收通过**：安装、静态检查、公开回归、真实本机 HTTP、Linux 进程生命周期和实际桌面 UI 均通过；不需要真实 PDK。
- **Linux 原生集成验收通过**：在合成验收基础上，使用用户授权的 Virtuoso/Calibre/PDK 和私有测试副本完成连接、导入、同步、服务器 DRC 与恢复验证。

没有真实原生证据时，只能声明第一级通过。区域 DRC、本地检查或合成测试均不等于 LVS、全芯片或工艺签核。

每项记录为 `PASS`、`FAIL` 或 `NOT_TESTED`，不得把未执行写成通过，也不得用 `N/A` 隐藏必须验收的项目。

## 2. 安全边界与准备工作

- [ ] 记录验收 Agent、日期、Linux 发行版/桌面、X11 或 Wayland、仓库绝对路径、`git log -1 --oneline` 和初始 `git status --short`。
- [ ] 使用独立 checkout 和独立运行目录。已有用户修改、工作区、`.env`、`.colayout`、artifacts、客户端 pending 和原生回执不得删除或覆盖。
- [ ] 不让测试安装指向用户可写生产工作区或当前原生目标；不连接服务器即可完成第 3～8 节。
- [ ] 不自动执行 `git add/commit/reset/clean`，不停止 Virtuoso，不关闭用户 APP，不启用工艺提案。
- [ ] 工具安装和 sudo 必须先获得用户授权；优先安装到 `$HOME`，不把额外工具装进仓库。
- [ ] 准备 Git、uv、Fontconfig 中文字体（建议 Noto Sans CJK SC）和 Qt 所需系统库。真实桌面检查准备 `wmctrl`；桌面启动项检查准备 `gtk-launch`。
- [ ] 创建新的验收输出目录，所有临时截图和日志放在其中：

```bash
export ACCEPTANCE_ROOT=.colayout/acceptance-$(date +%Y%m%d-%H%M%S)
mkdir -p "$ACCEPTANCE_ROOT"
```

## 3. 安装与环境证据

在全新的空白源码安装中执行：

```bash
./setup.sh
.venv/bin/python tools/check_environment.py
```

- [ ] `setup.sh` 成功，Python 为 3.13.x，依赖版本与锁定文件一致，未隐式升级 Bridge。
- [ ] `check_environment.py` 返回 `"status": "passed"`。
- [ ] `colayout-pdk`、`colayout-layout-agent`、`virtuoso` 三项技能均存在。
- [ ] Linux 中文 Fontconfig 检查通过；真实 Qt 所需的共享库可加载。
- [ ] 安装阶段没有 SSH、Virtuoso、Calibre 或生产版图写入。

建议补充一次路径包含空格和非 ASCII 字符的独立安装验证，确认启动器和客户端没有路径拆分错误。

## 4. 变更边界审查

- [ ] `model/editor/regions/geometry/autoroute/drc/adapter` 的版图语义、算法、权限、撤销和同步规则没有因平台适配改变。
- [ ] Agent API 契约、schema、HTTP 协议、工作区/工艺包格式和原生 SKILL wire protocol 未改变。
- [ ] Windows 的 `setup.ps1`、`tools/project.ps1`、`Start-CoLayout.cmd`、AppUserModelID、IPropertyStore、DWM 和 Job Object 路径仍保留。
- [ ] Linux 复用同一个 `StudioWindow`、Fusion 样式、主题、菜单、Dock、画布、对话框和快捷键，没有另写一套 UI。
- [ ] Windows 专属模块只在 Windows 分支导入；Linux 不出现 `msvcrt`、`ctypes.WinDLL` 或 PowerShell 调用。
- [ ] Linux 工作进程使用独立 session/process group；取消、超时和关闭覆盖整个进程组，包括启动器先退出的情况。
- [ ] 没有新增硬编码 PDK、层名、规则、主机、凭据或服务器路径。

## 5. 标准自动检查

执行并保存命令结果：

```bash
.venv/bin/python tools/check_environment.py
.venv/bin/ruff check colayout_lite tools
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests_lite -q
.venv/bin/python -m tools.build_customer_api_kit --check
.venv/bin/python tools/check_docs.py
.venv/bin/python tools/check_dependencies.py
.venv/bin/python tools/audit_release.py
git diff --check
```

- [ ] ruff 全部通过。
- [ ] pytest 全部通过；Linux 上 Windows 专属用例可明确跳过，其他跳过必须逐项解释。参考当前基线为 `622 passed, 1 skipped`，以后测试数可增加，但不能通过删除/跳过失败用例制造通过。
- [ ] SDK/schema 生成器返回 `different_files: []`。
- [ ] 文档链接、依赖来源和发布审计全部通过。
- [ ] `git diff --check` 无输出。
- [ ] 测试没有产生未解释的新源文件、凭据、原生版图或日志跟踪项。

## 6. 真实本机 HTTP 与独立客户端

在空白安装上运行：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/smoke_onboarding.py \
  --out "$ACCEPTANCE_ROOT/smoke-onboarding"
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/smoke_layout_client.py \
  --out "$ACCEPTANCE_ROOT/smoke-layout-client"
```

- [ ] 两项均返回 `"status": "passed"`；布局客户端输出目录必须是新目录。
- [ ] 独立客户端由 `python.sh` 启动，保留 `-E -S -X utf8` 语义和绝对项目解释器绑定。
- [ ] 工具发现、服务连接、区域 claim、layout/rules/reference/net 查询成功。
- [ ] 兄弟区并行画线/过孔成功，跨区修改被拒绝。
- [ ] Agent 自有 undo 不覆盖兄弟区编辑，reapply 恢复预期连接。
- [ ] 本地检查、PNG 截图、存档、handoff 成功。
- [ ] 没有有效服务器证据时，`release done` 被拒绝；不得清除 pending 或换 request ID 绕过。
- [ ] 客户端更新保留身份、pending、outputs 和自定义文件；`python.sh` 保持可执行。
- [ ] 输出明确记录 `server_drc_tested: false`、`real_pdk_tested: false`，不得据此声称原生验证通过。

## 7. Linux 进程与启动器验收

### 7.1 专项回归

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests_lite/test_linux_portability.py tests_lite/test_bootstrap_portability.py -q
```

- [ ] 进程查询同时匹配 `colayout_lite.workspace_app` 和精确 `--installation` 指针，不能误匹配其他模块/安装。
- [ ] 自动布线/只读 worker 是进程组领导者；SIGTERM 后可升级整组 SIGKILL。
- [ ] 子进程忽略 SIGTERM、启动器已先退出两种情况下均无遗留子进程。
- [ ] 客户端 POSIX 文件锁、桌面入口生成、中文字体和 Bridge PID 探测通过。

### 7.2 真实启动、状态和单实例

在正常图形会话中，确保没有本安装的 APP 正在运行：

```bash
./Start-CoLayout.sh
./tools/project.sh status
./Start-CoLayout.sh
wmctrl -c 'CoLayout Lite'
```

- [ ] 第一次启动后，`status` 能返回当前文档 ID、revision、`state`、`archive_folder` 和 `can_load`；首次描述符尚未就绪时只做有界重试。
- [ ] 第二次启动明确报告已有 APP，未创建第二个写者。
- [ ] 使用窗口管理器正常关闭，不 kill 生产进程；关闭后无本安装 APP/worker 遗留。
- [ ] 工作区锁、恢复文档和 pending 状态未被重置。

### 7.3 用户级桌面入口

仅生成文件、不修改 `$HOME` 时：

```bash
.venv/bin/python tools/install_workspace_shortcut.py \
  --out "$ACCEPTANCE_ROOT/colayout-lite.desktop"
```

实际安装和启动必须得到用户明确授权：

```bash
.venv/bin/python tools/install_workspace_shortcut.py
gtk-launch colayout-lite
./tools/project.sh status
wmctrl -c 'CoLayout Lite'
```

- [ ] `.desktop` 的 Exec、Path、Icon 指向当前 checkout，路径含空格时仍正确。
- [ ] 实际安装只写当前用户的 applications 目录，不需要 root，不修改系统级关联。
- [ ] `gtk-launch` 能打开同一 APP；`desktopFileName` 为 `colayout-lite`，图标和重启入口正确。

## 8. 实际图形界面验收

先运行真实 Qt 交互和截图：

```bash
env -u QT_QPA_PLATFORM .venv/bin/python tools/smoke_onboarding.py \
  --out "$ACCEPTANCE_ROOT/smoke-x11"
```

实际查看 `empty-app.png` 和 `process-help.png`，不能用 offscreen 截图代替真实桌面检查。当前环境没有 Wayland 时标记 `NOT_TESTED`，不得声称覆盖。

- [ ] 主窗口、菜单、工具栏、状态栏、画布、Dock、工艺中心、操作区、检查、自动布线、同步和 Agent 面板与 Windows 版本一致，无新增/缺失动作。
- [ ] 中文没有方框或乱码，字体回退有效；图标完整，对话框继承应用图标。
- [ ] 窗口最大化、平移缩放、选择、编辑、undo/redo、快捷键、剪贴板、模态对话框、紧凑主题切换和关闭/重开正常。
- [ ] 当前显示缩放比例下没有文字截断、控件重叠或不可达按钮；必要时补测 100%/150%/200% DPI。
- [ ] 空白工艺、真实样例或合成 fixture 的截图渲染一致，无黑屏、错位、残留高亮或缩放缓存异常。
- [ ] 实际 X11 图标验证通过：`desktopFileName=colayout-lite`、Linux identity 为真、Windows identity 为假。
- [ ] 若用户使用 Wayland，另在 Wayland 会话重复本节，记录窗口管理器及 Qt 平台插件。

允许不同窗口管理器导致原生标题栏和字体栅格化存在像素差异；不允许布局、控件、交互或功能缺失。

## 9. Windows 与 CI 回归

- [ ] CI 同时存在 Windows 和 Ubuntu job，均执行安装、静态检查、完整 pytest、SDK/schema、文档/依赖/发布审计和两个 smoke。
- [ ] Windows CI 全部通过；不能因 Linux 成功而删除 Windows 专属断言。
- [ ] Windows 实机使用 console Python 和 pythonw 检查主窗口、AppUserModelID、任务栏属性、DWM 标题栏、模态窗口、客户端 `python.cmd` 和正常关闭。
- [ ] 未实际触发 CI 或没有 Windows 实机时，对应项目标记 `NOT_TESTED`，不能仅依据代码审查写成通过。

## 10. 原生集成验收（仅在明确授权后）

没有合法 EDA/PDK、连接信息或授权测试副本时，本节全部标记 `NOT_TESTED`，结束于“Linux 合成功能验收”。

- [ ] SSH doctor、CIW 枚举/选择、Bridge 复用、Virtuoso PID 和工程目录绑定通过；多个 CIW 时由用户选择。
- [ ] 工艺样例采集、私有副本几何复现、提案提交与人工审核保持原流程；`pending_review` 不被写成已启用。
- [ ] 在授权私有副本上完成导入、本地编辑、显式同步、保存读回和基线更新。
- [ ] 服务器 DRC 在包含本地编辑的私有副本上运行；范围、指纹、终态和报告过滤证据有效。
- [ ] 取消、失败、保存读回失败和未知原生结果均保留 request/job ID、日志和 pending，并成功对账或明确停止。
- [ ] 不同时写同一原生目标，不重启 Virtuoso，不修改共享 PDK，不删除临时原生对象来绕过失败。
- [ ] 报告工具/PDK 版本、测试副本身份、执行阶段和脱敏回执；公开内容不包含私有版图或 foundry 规则。

## 11. 交付清理与最终门槛

- [ ] 正常关闭验收 APP 和对话框，确认无本安装 APP、只读 worker、自动布线或测试子进程遗留。
- [ ] 删除本次创建的临时 smoke、截图和状态日志；保留安装所需 `.colayout/installation.json`、依赖源码、当前工作区和需要交接的独立客户端。
- [ ] 再次运行 `git status --short`，与初始记录比较，仅预期源码/文档变化和运行数据存在。
- [ ] 所有强制项为 `PASS` 才能声明“Linux 合成功能验收通过”；任何强制项 `FAIL` 或 `NOT_TESTED` 都必须保留在结论中。
- [ ] 只有第 10 节也全部具备有效证据，才能声明“Linux 原生集成验收通过”。

## 12. 验收报告模板

```text
验收级别：Linux 合成功能 / Linux 原生集成
结论：PASS / FAIL / 部分完成
验收 Agent：
日期：
仓库与提交：
初始工作区状态：
Linux 发行版 / 桌面 / X11 或 Wayland：
Python / Qt / Bridge：
显示缩放：

| 清单节 | 结果 | 命令或证据 | 失败/未测原因 |
|---|---|---|---|
| 安装与环境 |  |  |  |
| 变更边界 |  |  |  |
| 标准自动检查 |  | pytest：；ruff：；审计： |  |
| HTTP 与客户端 |  | 两项 smoke： |  |
| 进程与启动器 |  |  |  |
| 实际图形界面 |  | 截图检查记录： |  |
| Windows 与 CI |  |  |  |
| 原生集成 |  |  |  |
| 清理 |  |  |  |

未覆盖范围：
已知问题及首个失败：
保留的工作区/客户端位置：
临时输出是否清理：
```

验收 Agent 必须报告首个失败及其证据，不能只报告最后一次成功命令，也不能用后续成功掩盖中间失败而未说明修复内容。
