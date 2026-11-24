# CoLayout layout workspace / 版图工作文件夹

This client talks only to the running desktop APP. Give a separate layout conversation access to this folder only, and start with AGENTS.md.

本客户端只连接运行中的桌面 APP。请为单独的版图对话仅授权本文件夹，先阅读 AGENTS.md。安装、工艺配置、源代码维护和直接服务器操作由初始化／维护对话负责。

[版图 Agent 提示词 / Layout prompt](PROMPT.md)可直接复制；填写操作区 ID 和任务即可。无需提供 SSH 或 root 账号。

Windows:

```powershell
.\python.cmd service.py connect
.\python.cmd api.py --client layout-1 tools
.\python.cmd api.py --client layout-1 workspace status
.\python.cmd api.py --client layout-1 claim "<user-provided-region-id>" --name "Layout agent"
.\python.cmd api.py --client layout-1 inspect layout
.\python.cmd api.py --client layout-1 inspect rules
.\python.cmd api.py --client layout-1 inspect parasitics --net "<net-name>"
```

Linux:

```bash
./python.sh service.py connect
./python.sh api.py --client layout-1 tools
./python.sh api.py --client layout-1 workspace status
./python.sh api.py --client layout-1 claim "<user-provided-region-id>" --name "Layout agent"
./python.sh api.py --client layout-1 inspect layout
./python.sh api.py --client layout-1 inspect rules
./python.sh api.py --client layout-1 inspect parasitics --net "<net-name>"
```

Cooperative Virtuoso sync helpers (strictly via the APP HTTP API, preserving disjoint drafts/leases): `./python.sh api.py --client layout-1 workspace pull` and `./python.sh api.py --client layout-1 workspace sync` (or `release --outcome done --sync-to-virtuoso`; substitute `.\python.cmd` on Windows).

Confirm exact command options with `api.py --help`, `api.py workspace --help`, and `api.py claim --help`; detailed edit fields are in the current tools response and docs/api-schema.json.

Each parallel agent uses a different --client name. Keep runtime/clients, pending requests and outputs when updating or reconnecting. If the APP has restarted, reconcile old unknown requests before resetting the old session; never replay edits automatically.

See docs/API.md for the workflow. Feedback belongs in feedback/ and should include a minimal request, region/revision, error code, expected/actual behavior and sanitized screenshots. Never include token/session_token or private process files in public issues.
