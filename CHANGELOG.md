# CoLayout 改动说明

## alpha：铺铜功能

在 `linux` 分支基础上加入单层矩形铺铜，人端界面与 Agent API 共用同一领域实现。

- 工具栏新增“铺铜”：选择金属层、目标网络和最小间距后，在画布拖出铺铜范围。
- 可显式选择当前层普通图形或过孔作为连接目标；未指定的同层金属一律作为障碍。
- 指定目标后删除未连接铜岛；未指定目标时保留所有独立铜岛。
- 遵守操作区边界、禁布区和原生黑盒策略，拒绝已知网络冲突、缺失几何和不安全结果。
- 间距不低于该层已配置的本地 spacing 规则，并增加制造网格取整保护。
- 无孔区域保留为普通多边形；有孔区域拆分为可编辑、可同步的多边形，单次最多 512 个。
- 整批铺铜共享一次撤销。Agent 通过 `edit_region` 的 `create_pour` 单项异步任务原子采用，客户端 schema 已同步更新。

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
- `alpha`：653 项公开测试通过，1 项跳过；包含 10 项铺铜行为、Agent 采用和 Qt 交互测试。
- 两个分支均通过 Ruff、客户端 schema、文档链接和发布/隐私审计。
- Linux 固定 Bridge 依赖来源、归档校验和许可证检查通过。
- `alpha` 的真实显示服务器 Qt 拖框测试通过。
- 使用 `uv 0.13.7` 和固定 Bridge 归档完成独立干净安装，`check_environment.py` 通过；`smoke_onboarding` 与 `smoke_layout_client` 均通过。smoke 使用合成几何和真实 APP/HTTP，不包含 SSH、服务器 DRC 或真实 PDK。
