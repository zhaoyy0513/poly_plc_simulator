# AGENTS.md

多协议 PLC **服务端**模拟器：一个管理平台 + 每台目标服务器上一个 agent。用户在页面上创建 site（= 一台服务器），在 site 下创建**互相独立**的模拟器实例（Modbus TCP / S7 / OPC UA），供外部 client 连接测试。

## 仓库现状

- 已实现：`uv` workspace + FastAPI 控制面 + 内嵌 Supervisor + `poly_sim_runtime` + React 前端 + Modbus/S7/OPC UA 三种协议，34 个 pytest 全绿，前端 `vite build` 通过。
- **远程部署已实现（P2）**：site 填了 `host` 就走 SSH，把源码包推上去、在目标机建一个 venv、每实例起一个进程；实测在 CentOS 8 上跑通（系统 Python 3.6 → 平台用 uv 装独立 CPython 3.11）。
- **link 实例已实现（P3）**：实例分两种身份（`kind`），`sim` = 平台部署并运行的 server；`link` = 平台作为 **client** 连一台别人正在跑的 Modbus server，不部署、不占端口，页面读写都是真实往返。见 `docs/architecture.md` §2.1。
- **仍未实现**：S7 / OPC UA 的 link（需要对应客户端库）、WS 会话、docker driver、登录权限。
- 架构决策见 `docs/architecture.md`。改结构前先读它；要偏离就先改文档。
- 真实验证过的命令见下方「命令」。不要写未验证的命令。

## 核心模型（最容易搞错）

```
平台
└── Site = 一台目标服务器 = 一个 agent
    ├── Server 实例 kind=sim  （modbus_tcp / s7 / opcua + bind_ip:port + runtime）
    │     └── Tag（自己的标签表 + 自己的内存）
    └── Server 实例 kind=link （target_host:target_port，无进程、不占端口）
          └── 对方设备的四个数据区，读写即真实往返
```

- **`kind` 是实例的第一维属性**，不是协议的一种。`sim` 与 `link` 在页面上共用「查看 / 编辑 / BIN-DEC-HEX」逻辑，差异只在数据来源（`poly_control_plane/link.py` vs `Supervisor`/`RemoteSupervisor`）。改页面或 API 时两种都要顾到。

- **实例之间零共享**：每个 server 实例独立数据、独立进程、独立端口。**不要引入跨协议共享内存/统一地址空间**——需求就是"多个互不影响的被测 server"。早期设计里的 Device / Binding 层已废弃，不要加回来。
- 平台对 `sim` 实例只是配置与进程管理，对 `link` 实例才真正当 client。PLC client 始终直连端口（sim）或由平台代为访问（link），两种都不经过 agent 转发。
- 隔离三级：机器（site）/ 进程（实例）/ 端口。一个实例崩了不能带走同机其他实例。
- 端口按**机器**作用域校验：本机（所有 `host` 为空的 site）共用一个内核端口表，每个远程 site 各算一台。`_port_conflict` 已按此实现，改它之前先想清楚「同机 / 异机」。

## 不可违反的架构约束

1. **端口只能由本机进程监听**，所以每台目标机都必须有一个属于我们的运行时进程；平台可以与其中之一同机，也可以独立部署。远程侧不常驻 agent：平台按需 SSH 上去起停进程（P2 的选择，见 `docs/plans/`）。
2. **平台不绑定协议端口、不代理流量**；agent 只做本机进程管理与状态/日志回传。
3. **agent 主动连出平台**（WS + 心跳），平台不反向连 agent。
4. **期望状态收敛**（`config_version` diff → 起/停/重建），不要加命令式 start/stop；唯一例外是显式 `restart`。
5. **断网要继续跑**：last-known-good 落本地磁盘，恢复后先对齐版本；容器/进程部署时该目录必须持久化。
6. **健康判定 = 进程存活 + 本机 TCP connect 端口成功**，只看进程会误报。
7. **不用第三方协议 server 镜像**：协议用自研封装（Modbus 手写 TCP 帧 / `python-snap7` / `asyncua`）。Docker 只作为我们自己的 runtime/agent 的部署方式。
8. **协议可用性是 capability 数据**：agent 上报 `{os, arch, capabilities}`；UI 置灰 + API 拒绝 + agent 自身拒绝，三层都要。
9. **S7 不再绑定 Windows**：`python-snap7 >= 3.3` 的 server 是纯 Python 实现，不需要 `snap7.dll`。可用性仍按 capability 实测上报，不硬编码平台判断。
10. **一个实例一个进程/容器**，不要一个进程里塞多个实例（会破坏"互不影响"）。
11. **端口映射不改变宿主机占用**：`-p 4841:4840` 仍要求宿主机 4841 被占用并放行；非标端口必须同步到 OPC UA 的 `endpoint_url` 和 S7 client 配置。

## Launcher

目标形态是 `native`（默认）与 `docker` 两种 driver，接口一致：起、停、重启、日志、健康检查。**当前只实现了 native**；docker 仍为规划项。**不要按实例生成 docker-compose 项目**——动态增删用 `docker run`/Docker API。本机实例日志落到 `data/logs/<server_id>.log`，远程实例日志在目标机 `~/.poly-sim/instances/<id>/runtime.log`。

本机版的 Supervisor 会写 `data/runtimes.json` 记录 runtime PID，启动时清理被硬杀后遗留的进程；容器/进程部署时 `data/` 必须持久化。

远程侧的对称物是 `poly_control_plane/deploy.py` 的 `RemoteHost` / `RemoteSupervisor`，方法签名与 `poly_agent.Supervisor` 对齐（`start/stop/restart/forget/state/values/logs/read_area/write_area`），差别只在 `start/restart` 多收一个 `SiteProbe`（用来决定载体）。页面与 API 不需要知道实例在哪台机器上。

## 协议层注意点

- 字节序显式配置：Modbus 浮点占 2 个寄存器且 AB-CD/CD-AB 两种词序；S7 默认大端。
- Modbus：**没有任何标签数据**（详见下方「约定」），整个实例就是四个原始数据区。单实例可用单端口（多 unit_id 也可）。四个区按完整 16 位地址空间（0–65535）固定分配，没有配置的地址一律返回 0，**服务端不产生任何模拟值**——只有 client 写入或页面写入才会变。页面「数据区」通过 runtime `/area` 读写原始寄存器/线圈。
- S7：面积映射（DB/M/I/Q）以字节为单位，**标签地址重叠必须校验**；rack/slot 可配；并发连接有限。
- OPC UA：`endpoint_url` 必须可配（自动探测会给出 127.0.0.1 或容器名）；地址空间按标签表生成。
- agent 会在网络上开任意端口，必须带可吊销 token；未认领的 agent 不接受配置。
- 端口按 site 分段规划（如 site 12 用 5020-5099），避免实例增多后撞端口。

## link 实例（P3）

平台作为 **client** 连一台别人正在跑的 Modbus server。核心文件：
`packages/proto-modbus/poly_modbus/client.py`（`ModbusTcpClient`）与
`apps/control-plane/poly_control_plane/link.py`（`LinkSupervisor`）。

- **协议只支持 Modbus TCP**。S7 当 client 在 Windows 上还需要 `snap7.dll`，OPC UA 需要 asyncua client 封装，都没做。
- **连接前必须先通**：`create_server` / `update_server` 对 link 会先 `probe()` 一次，失败就拒绝保存；`POST /api/sites/{id}/link-test` 是不落库的探测入口。改指向失败时不影响原连接。
- **`port` 必须等于 `target_port`**，跳过本机端口冲突与 bind 检查（没有进程在监听）。
- **三种错误必须分开**（`poly_modbus.client`）：
  - `ModbusError` — 链路级（连不上/超时/掉线），**只有这种**才把连接判成 error；
  - `ModbusException` — 设备应答并拒绝，是活着的证明，只进历史；
  - `ModbusReadOnlyError` — 只读区写入在 client 侧挡下，请求不离开本机。
  混在一起会让一次被拒的写入把一条好连接标红 5 秒（真实踩过，有回归测试）。
- **轮询节奏**：健康 1s，不健康退避 5s；连接历史上限 60 条。
- **写入没有撤销**：页面弹窗要求勾选「我确认写入外部设备」，`input` / `discrete` 在 link 实例上不提供写入入口。
- **视觉区分不把整卡染色**：协议徽章仍占协议色（MB 蓝 / S7 琥珀 / UA 紫）。site 页按身份分成上下两个分组（「自建实例」/「连接实例」），再用更大的身份 chip（自建=低饱和钢蓝 / 连接=青）、卡片左侧 3px 身份条、虚线与副标题区分；钢蓝避开 S7 的琥珀和告警色，身份色不覆盖整卡，去掉颜色后仍有分组标题和文字可读。改这里前先读 `docs/architecture.md` §2.1。
- **停用/启用是期望状态，不是 start/stop 命令**：页面写 `PATCH /api/servers/{id}` 的 `enabled`。`false` 时 sim 停进程并释放端口、link 断开；控制面重启只恢复 `enabled=true` 的实例。`POST /restart` 对已停用实例返回 409，不要在前端绕过这个状态直接起进程。停用后的 `/values` 返回 stopped，`/area` / `/write` 返回 409；`LinkSupervisor` 的读写也要拒绝，否则 `ModbusTcpClient` 会因 socket 关闭而自动重连，一次刷新就能把停用的 link 偷偷接回去。

## 目标仓库结构

```
apps/control-plane/       FastAPI：site/server/tag CRUD、页面 API、内嵌 Supervisor、SSH 部署（ssh.py / deploy.py）
apps/web/                 React + Vite + TS（单页，构建产物由 control-plane 托管）
apps/agent/               Supervisor：本机进程监督、日志、健康检查
apps/sim-runtime/         单实例运行时：读自己 config，起一个协议 server
packages/plc-core/        标签表、编解码、仿真值（协议无关，必须可单测）
packages/contracts/       平台 ⇄ agent 的 Pydantic 契约，两侧同版本
packages/proto-{modbus,s7,opcua}/
docs/
```

`deploy/`（docker-compose）尚未创建，随 docker driver 一起加。

## 约定

- Python 用 `uv`；类型注解 + Pydantic v2 作为平台/agent 契约。
- **workspace 包名不等于目录名**（踩过）：`packages/plc-core` → `poly-core`，`packages/proto-modbus` → `poly-proto-modbus`（同理 `poly-proto-s7` / `poly-proto-opcua`），其余是 `poly-<目录名>`。加依赖时先 `head -2 packages/<目录>/pyproject.toml` 确认，写错的表现是 `uv run` 报 `references a workspace in tool.uv.sources ... but is not a workspace member`，而直接用 `.venv/Scripts/python` 跑测试不会暴露（venv 是旧的）。
- 前端用 React 19 + Vite + TS + zustand；类型手写在 `apps/web/src/types.ts`（暂未接 OpenAPI 生成）。UI 用 MD3 风格 token 手写组件，不引入 mdui Web Components。
- **Modbus 实例没有「实时变量」「标签表」**（`ServerPage` 里按 `isModbus` 分支），第二个 tab 按身份分：`sim` 是「**日志**」，`link` 是「**连接**」（轮询历史 / 往返延迟 / 错误原因，`GET /logs` 返回体带 `link: true`）。S7 / OPC UA 实例页是「实时变量」「标签表」「日志」三个 tab。不要给 Modbus 加回「实时变量」——它没有名字可读，数据区就是它唯一的视图。
- 数据区交互：单元格双击（或回车/空格）弹窗写入，输入支持 BIN / DEC / HEX 三种进制并带范围校验；线圈/离散输入只能是 0/1。网格 1 秒轮询，`/api/servers/{id}/area` 的分页是每页 50 个。
- 主题：默认深色，左侧栏底部按钮切换到浅色，选择存在 localStorage `poly.theme`（`dark` | `light`）。浅色只是 `:root[data-theme='light']` 重定义 token，不要在组件里写死颜色。
- 开发库 SQLite（当前唯一支持），部署 Postgres 是后续项；schema 变更走 Alembic（尚未接入）。
- 运行态（监听状态、连接数、当前值）不进配置表，由 runtime 的 `/state` 经 Supervisor 拉取；原始数据区走 `/area`，不要为了看地址范围往标签表里塞占位标签。
- **site = 一台目标机器**：`host` 为空即本机（走内嵌 Supervisor），否则持有 SSH 访问信息。**没有 Alembic**，`create_all()` 不会给旧表加列，schema 变更要同时在 `poly_control_plane/migrate.py` 的 `EXPECTED_COLUMNS` 里登记（启动时会按 `PRAGMA table_info` 补齐）。
- **凭据**：site 密码用 `data/.secret` 里的密钥加密后存 `site.ssh_secret`，API 与 UI 一律不返回明文，前端只拿到 `ssh_configured` 布尔值。不要加「把密码读回来」的接口。
- SSH 相关逻辑集中在 `poly_control_plane/ssh.py`（连接按 site 复用、`run_probe` 探测系统/发行版/Python/uv/docker/端口），部署与远程进程管理在 `poly_control_plane/deploy.py`。三个已踩过的坑，改这两个文件前务必知道：
  - **不要用通道 EOF 判断远程命令结束**。命令留下后台进程时 channel 不关，`stdout.read()` 会一直阻塞。退出码是内联在 stdout 里的（`__poly_rc=`），读到标记就停 —— 见 `_drain_channel`。
  - **SFTP 不展开 `~`**，写文件前必须先把部署目录 `cd ... && pwd` 解析成绝对路径（`RemoteHost.resolve()`）。
  - **`&` 之后的 `$!` 不一定是目标进程**：`( PYTHONUNBUFFERED=1 exec ... ) &` 里的 `exec` 不能删，否则 kill 掉的是外层 shell，运行时会变成孤儿继续占端口。
- **部署载体由机器决定，UI 只告知**（`deploy.decide_carrier`）：系统 Python ≥ 3.10 → native；否则用 uv 装独立 CPython；docker 只留口子。**不要在前端加「选部署方式」的开关**——不满意应该去修机器。
- **Modbus 实例没有标签表，也不做仿真**：`ServerConfig.tags` 恒为空，创建时传标签会被 API 拒绝（`PUT /tags` 同样拒绝），`poly_modbus` 适配器不实现 `read_tag/write_tag`，数据区永远是全 0 起步；`sample_tags` 对 Modbus 无效，只有 S7 / OPC UA 会用到它。修改这条语义要同步改 docs 与本条。
- S7 / OPC UA 实例才有标签表：默认**空标签表**（全 0 数据区），`sample_tags=true` 才生成 4 个演示变量；UI 复选框默认不勾选。变量可按 `sine/random/ramp/toggle` 仿真。
- 写值、改配置落 audit log（`/api/audit`）。
- 落地顺序：本机版已完成 `plc-core` + Modbus → `sim-runtime` + Supervisor → 平台 + 前端 → 第二 site 隔离 → S7 → OPC UA → SSH 远程部署（P2）→ link 实例（P3）；剩余 S7 / OPC UA 的 link、docker driver、登录权限。

## 命令

除特别说明外都在仓库根目录执行；以下命令已在 Windows 11 + PowerShell 验证。

- 安装 Python 依赖（uv workspace）：`uv sync --group dev`
- 跑全量测试：`uv run pytest`
- 跑单个测试：`uv run pytest -k <关键字>`
- 启动控制面（同时承担本机 agent + 前端静态托管）：`uv run python -m poly_control_plane --host 0.0.0.0 --port 25011`
  - 页面：`http://<本机局域网IP>:25011/`；API 同端口 `/api`
  - 只有在 `apps/web/dist` 存在时才会托管页面；构建后需重启 control-plane
  - 启动时自动拉起 DB 里已有的实例；正常退出会停掉它们
- 前端（另有 `apps/web` 目录）：
  - `npm install`
  - `npm run dev` — 绑 `0.0.0.0:5173`，`/api` 代理到 `127.0.0.1:25011`
  - `npm run build` — 先 `tsc --noEmit` 再打包到 `apps/web/dist`
  - `npm run typecheck`
- 单实例 runtime 手动调试（一般不需要，control-plane 会自动拉起）：`uv run python -m poly_sim_runtime --config data/configs/<id>.json --control-port 18080`

端口：本机已被 Windows 保留的端口段会导致 `WinError 10013`；已验证可用的起始端口是 15020（Modbus）、15102（S7）、15400（OPC UA）。同一台机器上端口不可重复（全局校验）。

清理：停掉 control-plane 后删除 `data/`（SQLite、配置、日志、PID 文件）即可回到空状态。
