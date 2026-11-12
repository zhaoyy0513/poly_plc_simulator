# AGENTS.md

多协议 PLC **服务端**模拟器：一个管理平台 + 每台目标服务器上一个 agent。用户在页面上创建 site（= 一台服务器），在 site 下创建**互相独立**的模拟器实例（Modbus TCP / S7 / OPC UA），供外部 client 连接测试。

## 仓库现状

- 已实现：`uv` workspace + FastAPI 控制面 + 内嵌 Supervisor + `poly_sim_runtime` + React 前端 + Modbus/S7/OPC UA 三种协议，pytest 全绿（数量按 `uv run pytest` 实际输出），前端 `vite build` 通过。
- **远程部署已实现（P2）**：site 填了 `host` 就走 SSH，把源码包推上去、在目标机建一个 venv、每实例起一个进程；实测在 CentOS 8 上跑通（系统 Python 3.6 → 平台用 uv 装独立 CPython 3.11）。
- **控制面跨平台，远程部署只做 Linux（Windows 明确排除）**：控制面 Windows / Linux / macOS 都能跑，本机 site 跟着平台走；**Windows 机器不参与远程部署**——它自己跑控制面、host 留空走本机模式。远程 site 靠 `uname` / `ss` / `pkill` / `nohup` 管理进程，所以 `ssh.probe_target` 第一步 `_platform_verdict()` 就下结论：Linux 放行，Windows（无 `uname`，或 Git-Bash 的 `MINGW64_NT-*`，用 `cmd /c ver` 确认）返回「Windows 不做远程部署，请在本机运行控制面」，其他 POSIX（如 Darwin）返回「只在 Linux 上支持」，**都不再继续跑后续命令**——否则报错会读成"机器坏了"而不是"不支持"。拒绝时 `ok=False` 但 `os` 保留，前端据此显示成琥珀色规则提示而不是红色连接失败。
- **link 实例已实现（P3）**：实例分两种身份（`kind`），`sim` = 平台部署并运行的 server；`link` = 平台作为 **client** 连一台别人正在跑的 server（Modbus TCP / S7 / OPC UA），不部署、不占端口，页面读写都是真实往返。见 `docs/architecture.md` §2.1。
- **仍未实现**：docker driver、WS 推送（现在轮询）；登录与两级权限已实现。
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
- **一台机器可以起任意多个实例，同协议也可以**（2 个 Modbus、2 个 OPC UA 都是正常用法）。**不要再加「每 site 每协议只能一个」的规则**——那条规则写过又删掉了，它挡的是合理需求，而多实例本来就不冲突：各自独立进程、独立数据面、独立 config/log 文件、独立控制端口。真正的约束只有一个：**端口不能重复**。
- **端口守卫三段，缺一不可**：数据库冲突（`_port_conflict`，按机器分组）→ 本机真 bind（`poly_core.net.can_bind`）→ 远程实时 `ss -ltn`（`RemoteSupervisor.port_in_use`，不用可能过期的站点探测缓存）。前端在端口输入框失焦时调 `GET /api/sites/{id}/port-check` 提前告知，那只是**提示**，答案会过期，最终以保存时的检查为准。只有端口真的变了才做实时检查——端口没变时实例自己正占着它，bind 会假报冲突。
- **监听绑定必须走 `poly_core.net.bind_for_listening`**：POSIX 的 `SO_REUSEADDR` 只回收 TIME_WAIT，Windows 的同名选项却允许别的进程抢占你正在用的端口——直接用它会让两个实例都"启动成功"、只有一个收到请求。Windows 需要 `SO_EXCLUSIVEADDRUSE`，POSIX 需要 `SO_REUSEADDR`。预检与真监听共用这一个函数，"查的时候说空闲"和"绑的时候能绑上"才不会打架。

## 不可违反的架构约束

1. **端口只能由本机进程监听**，所以每台目标机都必须有一个属于我们的运行时进程；平台可以与其中之一同机，也可以独立部署。远程侧不常驻 agent：平台按需 SSH 上去起停进程（P2 的选择，见 `docs/plans/`）。
2. **平台不绑定协议端口、不代理流量**：本机由内嵌 `poly_agent.Supervisor` 管进程，远程由 SSH 按需起停（`deploy.RemoteSupervisor`），两侧方法签名对齐。页面与 API 不需要知道实例在哪台机器上。
3. **远程侧不常驻我们的进程**——没有 WS agent、没有心跳注册。平台每次按需 SSH 上去查/起/停，连接按 site 复用（`SshSessionPool`）。早期规划里的「agent 主动连出平台」已废弃，不要再按它设计。
4. **`enabled` 是期望状态，不是 start/stop 命令**：页面写 `PATCH /api/servers/{id}` 的 `enabled`，控制面重启只恢复 `enabled=true` 的实例。不要加命令式 start/stop；唯一例外是显式 `restart`，且对停用实例返回 409。
5. **每个实例的配置都落磁盘，重启据此重建**：本机 `data/configs/<id>.json`、远程 `~/.poly-sim/instances/<id>/config.json`；进程被硬杀后本机按 `data/runtimes.json` 清单接管或清理，远程按 pid 文件 + 配置路径匹配（`pkill -f "... --config {dir}/config.json"`，只匹配自己那一个）。
6. **健康判定 = 进程存活 + 本机 TCP connect 端口成功**，只看进程会误报。
7. **不用第三方协议 server 镜像**：协议用自研封装（Modbus 手写 TCP 帧 / `python-snap7` / `asyncua`）。Docker 只作为我们自己的 runtime/agent 的部署方式。
8. **协议可用性是 capability 数据**：`system.probe_protocols()` 在控制面本机实测，UI 置灰 + API 拒绝两层都要；远程 sim 的目标机装什么由部署时的依赖决定，不硬编码平台判断。
9. **S7 不再绑定 Windows**：`python-snap7 >= 3.3` 的 server 是纯 Python 实现，不需要 `snap7.dll`。可用性仍按 capability 实测上报，不硬编码平台判断。
10. **一个实例一个进程/容器**，不要一个进程里塞多个实例（会破坏"互不影响"）。
11. **端口映射不改变宿主机占用**：`-p 4841:4840` 仍要求宿主机 4841 被占用并放行；非标端口必须同步到 OPC UA 的 `endpoint_url` 和 S7 client 配置。
12. **远程部署只针对 Linux；Windows 是本机运行，不做远程目标**。不要为 Windows 目标机加 SSH 兼容层（cmd/PowerShell 的引号、路径、进程管理都不一样），那条路只会长出难查的假故障。用户想在 Windows 上跑，就让他直接在那台机器上跑控制面。
13. **控制面不是数据面**：正常退出、被硬杀、IDE/服务管理器收走进程树，都不应该带走本机和远程的 sim runtime；link 只是控制面进程内的 client 连接，退出时断开是正常的。下次启动先 adopt（pid + 创建时间 + `/state` 的 `server_id`/`config_hash`），配置变了、进程死了、实例停用/删除才停掉重建。只有 `--stop-on-exit` / `POLY_STOP_RUNTIMES_ON_EXIT=1` 显式要求时才在退出时全停（本机）。Windows 上 runtime 额外尝试 `CREATE_BREAKAWAY_FROM_JOB`，POSIX 用 `start_new_session=True`；整机重启不保留内存数据。

## Launcher

目标形态是 `native`（默认）与 `docker` 两种 driver，接口一致：起、停、重启、日志、健康检查。**当前只实现了 native**；docker 仍为规划项。**不要按实例生成 docker-compose 项目**——动态增删用 `docker run`/Docker API。本机实例日志落到 `data/logs/<server_id>.log`，远程实例日志在目标机 `~/.poly-sim/instances/<id>/runtime.log`。

本机版的 Supervisor 会写 `data/runtimes.json`（v2：`pid`、创建时间、控制端口、`config_hash`、`started_at`）。正常退出只 detach；下次启动先按清单 adopt，配置一致就复用同一进程，配置变了、进程死了或实例已停用/删除才清理重建。`--stop-on-exit` / `POLY_STOP_RUNTIMES_ON_EXIT=1` 才在退出时停掉本机运行时。容器/进程部署时 `data/` 必须持久化。

远程侧的对称物是 `poly_control_plane/deploy.py` 的 `RemoteHost` / `RemoteSupervisor`，方法签名与 `poly_agent.Supervisor` 对齐（`start/stop/restart/forget/state/values/logs/read_area/write_area`），差别只在 `start/restart` 多收一个 `SiteProbe`（用来决定载体）。远程 `start_instance` 同样先尝试 adopt，确认不是自己的进程才 `pkill -f` 唯一 config 路径。页面与 API 不需要知道实例在哪台机器上。

## 协议层注意点

- 字节序显式配置：Modbus 浮点占 2 个寄存器且 AB-CD/CD-AB 两种词序；S7 默认大端。
- Modbus：**没有任何标签数据**（详见下方「约定」），整个实例就是四个原始数据区。单实例可用单端口（多 unit_id 也可）。四个区默认按完整 16 位地址空间（0–65535）分配，没有配置的地址一律返回 0，**服务端不产生任何模拟值**——只有 client 写入或页面写入才会变。页面「数据区」通过 runtime `/area` 读写原始寄存器/线圈。
- Modbus 的 `options.register_count`（可选，四个区共用）把每个区缩到 N 个点：**分配长度就是上限**，所以既有的越界检查会自动回 `ILLEGAL_ADDRESS`（02），不需要再加分支。要改这个语义就改 `poly_modbus.server.area_size()`，别在协议处理里另写一份判断。页面通过 `/area` 响应里的 `limit` 显示「有效 0–N」并在请求前拦截；link 实例恒为 65536——对端的映射由对端说了算。
- **Modbus 引用地址有两种宽度，都表示同一个地址**：5 位经典块（`40001` = 保持寄存器偏移 0，区间 40001–49999 覆盖偏移 0–9998）与 6 位扩展块（`400001` = 偏移 0，覆盖到 465536）。换算逻辑集中在 `apps/web/src/modbusAddress.ts`（`parseStart` / `refText` / `altRefText`），改之前先跑 `npm run check:address`（31 条对照表，node 需 ≥ 22.6）。**踩过的坑**：原来只认 6 位，输入 `40001` 被当成"偏移 40001"，页面显示的寄存器和设备手册完全不是同一个——1 位之差，肉眼很难发现。线圈用前导 0 区分（`00001` 是引用，`1` 是偏移）。
- S7：面积映射（DB/M/I/Q）以字节为单位，**标签地址重叠必须校验**；rack/slot 可配；并发连接有限。
- OPC UA：`endpoint_url` 必须可配（自动探测会给出 127.0.0.1 或容器名）；地址空间按标签表生成。
- agent 会在网络上开任意端口，必须带可吊销 token；未认领的 agent 不接受配置。
- 端口按 site 分段规划（如 site 12 用 5020-5099），避免实例增多后撞端口。

## link 实例（P3）

平台作为 **client** 连一台别人正在跑的 server。核心文件：
`packages/proto-{modbus,s7,opcua}/poly_*/client.py` 与
`apps/control-plane/poly_control_plane/link.py`（`LinkSupervisor`）、
`apps/control-plane/poly_control_plane/link_clients.py`（协议适配层）。

- **三种协议都支持 link**（`link_clients.py` 的 `build_link_client` 选路）：Modbus 走手写 `ModbusTcpClient`，S7 走 `poly_s7.S7Client`（snap7 3.x 纯 Python，不需要 dll），OPC UA 走 `poly_opcua.OpcUaClient`。
- **OPC UA 的 client 是异步库 + 同步接口**，靠 `poly_opcua/client.py` 里**每实例一条常驻事件循环线程**（`asyncio.new_event_loop` + `run_forever`，协程用 `run_coroutine_threadsafe` 提交）桥接。三条不能改的细节：
  - **`close()` 只断会话，不停线程**。停用链路是 `close()`、启用链路是 `probe()`，若 close 顺手停掉 loop，一次「停用 → 启用」之后就永远连不上。线程由 `dispose()` 收，只在实例被删除时由 `LinkSupervisor.forget()` 调用。
  - **`auto_reconnect` 保持 False**（asyncua 的默认值）。否则停用的 link 会被一次页面轮询偷偷接回去，跟 Modbus 那条铁律是同一个坑。
  - **`connected` 不能用 `Client.check_connection()`**——那是 async 函数，直接 `bool()` 会得到一个永远为真的 coroutine。现在读 `client.state.value == "connected"`。
- **`BadNodeIdUnknown` / `BadTypeMismatch` 是「设备拒绝」不是「掉线」**：asyncua 的 `UaError`（含 `UaStatusCodeError` 与本地解析错误 `UaStringParsingError`）一律翻成 `LinkDeviceError`；只有 `TimeoutError` / `ConnectionError` 之类才翻成 `LinkConnectionError`。搞反的后果是打错一个 NodeId 就让整条连接标红，用户去找根本不存在的网络问题。
- **OPC UA 没有原始数据区**：`read_area` / `write_area` 抛 `LinkDeviceError`，和 S7 一样，视图只有标签表。
- **`TagDef.node_id` 只对 link 有意义**：sim 侧的 NodeId 就是 `ns=<我们注册的索引>;s=<tag.name>`（`OpcUaAdapter` 用 `ua.NodeId(tag.name, index)` 建节点），填了 `node_id` 会被忽略。link 侧留空则按 `ns=<LinkOptions.namespace_index>;s=<name>` 推导（`poly_opcua/variants.node_id_for`），所以连自家模拟器零配置。**不要**让 sim 也去读 `node_id`，那会拼出双层命名空间的怪地址。
- **`LinkOptions.namespace_index` 默认 2**：0 是 OPC UA 标准、1 是对方 server 的应用命名空间，我们自己的 server 固定落在 2。这个数字对方说了算、猜不出来，所以 `link-test` 的返回体带 `namespaces`（对方命名空间表），弹窗据此列出来让用户照抄。
- **三种 link 的视图不一样**：Modbus link 没有标签表（`read_tags` 直接抛 `LinkDeviceError`），只有四个原始数据区；S7 link 与 OPC UA link 没有原始数据区（`read_area` 抛 `LinkDeviceError`），只有标签表。UI 按协议分支，不要假设三者同形。
- **连接前必须先通**：`create_server` / `update_server` 对 link 会先按协议建 client 再 `probe()` 一次，失败就拒绝保存；`POST /api/sites/{id}/link-test?protocol=...` 是不落库的探测入口。改指向失败时不影响原连接。
- **`port` 必须等于 `target_port`**，跳过本机端口冲突与 bind 检查（没有进程在监听）。
- **三种错误必须分开**（`link_clients.py`）：
  - `LinkConnectionError` — 链路级（连不上/超时/掉线），**只有这种**才把连接判成 error；
  - `LinkDeviceError` — 设备应答并拒绝，是活着的证明，只进历史；
  - `LinkStoppedError` — 实例已停用，读写一律拒绝（防自动重连）。

  下层 `poly_modbus` 仍抛 `ModbusError` / `ModbusException` / `ModbusReadOnlyError`，adapter 负责翻译成上面三种。混在一起会让一次被拒的写入把一条好连接标红 5 秒（真实踩过，有回归测试）。
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
- 主题：默认跟随系统（`prefers-color-scheme`），系统明确要求深色时用深色，否则用浅色；左侧栏底部按钮可切换，选择存在 localStorage `poly.theme`（`dark` | `light`）。浅色只是 `:root[data-theme='light']` 重定义 token，不要在组件里写死颜色。
- 确认弹窗统一用 `components/ConfirmModal.tsx` 的 `ConfirmProvider` + `useConfirm()`：`const ok = await confirm({ title, message, danger: true })`。危险操作用 `danger: true`（红色按钮、默认聚焦「取消」）。不要再用 `window.confirm` / `window.alert`。
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
- 落地顺序：本机版已完成 `plc-core` + Modbus → `sim-runtime` + Supervisor → 平台 + 前端 → 第二 site 隔离 → S7 → OPC UA → SSH 远程部署（P2）→ link 实例（P3，三种协议全通）；剩余 docker driver、WS 推送。

## 命令

除特别说明外都在仓库根目录执行；以下命令已在 Windows 11 + PowerShell 验证。

- 安装 Python 依赖（uv workspace）：`uv sync --group dev`
- 跑全量测试：`uv run pytest`
- 跑单个测试：`uv run pytest -k <关键字>`
- 启动控制面（同时承担本机 agent + 前端静态托管）：`uv run python -m poly_control_plane --init-admin admin1 --host 0.0.0.0 --port 25011`
  - 页面：`http://<本机局域网IP>:25011/`；API 同端口 `/api`
  - 只有在 `apps/web/dist` 存在时才会托管页面；构建后需重启 control-plane
  - 启动时自动拉起 DB 里已有的实例；正常退出只 detach，下次启动 adopt；只有 `--stop-on-exit` / `POLY_STOP_RUNTIMES_ON_EXIT=1` 才停
- 前端（另有 `apps/web` 目录）：
  - `npm install`
  - `npm run dev` — 绑 `0.0.0.0:5173`，`/api` 代理到 `127.0.0.1:25011`
  - `npm run build` — 先 `tsc --noEmit` 再打包到 `apps/web/dist`
  - `npm run typecheck`
- 单实例 runtime 手动调试（一般不需要，control-plane 会自动拉起）：`uv run python -m poly_sim_runtime --config data/configs/<id>.json --control-port 18080`

端口：本机已被 Windows 保留的端口段会导致 `WinError 10013`；已验证可用的起始端口是 15020（Modbus）、15102（S7）、15400（OPC UA）。同一台机器上端口不可重复（全局校验），但同一协议可以开多个实例，各占一个端口即可。Linux 上失败是 `EADDRINUSE`（有别的进程占着，`ss -ltn` 看是谁）或 1024 以下的特权端口 `Permission denied`。

清理：先让实例停掉（`--stop-on-exit` 或页面停用），再停 control-plane，删除 `data/`（SQLite、配置、日志、清单）即可回到空状态。
