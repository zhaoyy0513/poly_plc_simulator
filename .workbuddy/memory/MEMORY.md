# 项目长期记忆：poly_plc_simulator

## 产品定位（v0.3，2026-10-22 定型）

一个**统一管理平台**（不是每台服务器各部署一套），管理若干台真实服务器上的 PLC 模拟器实例，同时能作为 client 连接别人已有的 PLC server。

- **site = 一台真实服务器**，持有 SSH 访问信息（host / 端口 / 账号 / 密码密文 / 部署目录）。`host` 为空表示「本机」，走内嵌 Supervisor。
- **实例有两种身份**（`kind`，已实现）：
  - `sim` = 我们部署并运行在目标机上的 server
  - `link` = 平台作为 client 连到别人的 server（不部署、不占端口）
- 两种实例在页面上共用「查看 / 编辑 / BIN-DEC-HEX」逻辑，差异只在数据来源。
- 详见 `docs/plans/2026-10-22-remote-site-server-client-design.md` 与 `docs/architecture.md` §2.1。

## 已定下的技术选择（不要反复改）

- 部署：**SSH 推送 native 脚本**，目标机不常驻 agent；进程管理纯 SSH 按需查询，连接按 site 复用。
- **远程载体 = native 进程**：目标机 Python ≥ 3.10 直接用；太旧则用 **uv 装独立 CPython 3.11**（无 root，30s，只写 `~/.local` + 部署目录）。**docker 不做**（共享机器上 `docker.sock` 通常没权限，实测 45 的 `prime` 用户就是），只留 `carrier=docker` 的口子并标 `ready=False`。
- 载体由机器能力决定，**UI 只告知不提供选择**；用户不满意就自己去修机器再看。
- 远程 runtime 控制端口只绑目标机 127.0.0.1，平台经 SSH `direct-tcpip` 访问。
- 核心抽象 `DataSource`：LocalRuntime / SshRuntime / ModbusClient / OpcUaClient 四个实现（**link 侧已落地 ModbusClient + S7Client**，见下节）。
- 单人多环境，**不做登录与权限**（正交，后加）。
- link 协议：Modbus TCP（手写 MBAP + FC 1/2/3/4/5/6/15/16）与 S7（snap7 3.x 纯 Python，无 dll 依赖）；OPC UA 的 link 未做（缺客户端库）。
- **Modbus 实例无标签表、无仿真值**，只有四个原始数据区（0–65535），值只由 client 或控制台写入产生；实例的 tab 是「数据区」+「日志」（sim）/「连接」（link）。

## link 实例的三条铁律（真实踩过的坑）

1. **连接前必须先通**：创建 / 改向 link 都要先 `probe()`，失败拒绝保存，避免从出生就是红的卡片。
2. **三种错误必须分开**：`LinkConnectionError`（链路级，唯一判 error 的）/ `LinkDeviceError`（设备应答并拒绝，是活着的证明）/ `LinkStoppedError`（停用后拒绝，防止自动重连）。这是 `link_clients.py` 的职责，`LinkSupervisor` 不認識协议细节。下层 `poly_modbus` 仍抛 `ModbusError` / `ModbusException` / `ModbusReadOnlyError`——两层故意分开，adapter 负责翻译。
3. **写入没有撤销**：link 写入弹窗要勾「我确认写入外部设备」；`input`/`discrete` 不提供写入入口。

## link 的协议适配层（P4 起）

- `link_clients.py` 是唯一知道协议的地方：`build_link_client(protocol)` 返回
  `ModbusLinkClient` 或 `S7LinkClient`，都实现 `LinkClient` 协议。
- **link 支持 Modbus TCP + S7**；OPC UA 没做。S7 用 `poly_s7.S7Client`（snap7 3.x 纯 Python）。
- **Modbus link 无标签表**（`read_tags` 直接抛 `LinkDeviceError`），S7 link **无原始数据区**
  （`read_area` 抛 `LinkDeviceError`）——两者的页面视图因此不同。
- `LinkOptions`：`unit_id` 只有 Modbus 用，`rack`/`slot` 只有 S7 用。
- 前端 `CreateServerModal` 的 link 分支可选协议（仅 MB/S7；可用性看**控制面本机**的
  `system.protocols`，因为 link 由控制面亲自拨出，与 site 机器无关）；S7 link 默认种
  示例变量表（没有原始数据区兜底）；`ValueTable` 与 `DataAreaView` 都有 link 写入确认。
- **S7 link 的 demo 布局**（`_default_tags`）：temperature@DB1+0 / pressure@DB1+4 /
  counter@DB1+8 / **running 在 M 区**。验证对端时从 API 读回这张表再建远端，别手抄。

## 端口护栏与多实例（创建/更新时生效）

- **一个 site 下可以有任意多个实例，同协议也可以**（2 Modbus + 2 OPC UA 是正常需求）。
  「每 site 每协议只能一个」（`_protocol_conflict`）写过又删了——多实例本来就不共享任何
  东西（独立进程/内存/数据面、独立 config+log、独立控制端口），**端口才是唯一硬约束**。
  别再把这规则加回来。
- 端口要过三关：DB 冲突（`_port_conflict`，按机器分组）→ 本机真 bind
  （`poly_core.net.can_bind`）→ 远端实时 `ss -ltn`（`RemoteSupervisor.port_in_use`，
  **不用站点缓存探测**，可能过期几分钟）。
- **只有端口真变了才做实时检查**：端口没变时实例自己占着它，live bind 会把每次
  改名都拒成 409。
- 前端在端口输入框**失焦**时调 `GET /api/sites/{id}/port-check` 提前告知（黄字=占用 /
  绿字=可用），但**不用它卡提交**——答案会过期，服务端保存时才是判决。
- **绑定的 SO_* 选项必须按平台分叉**（`packages/plc-core/poly_core/net.py`）：
  POSIX 用 `SO_REUSEADDR`（只回收 TIME_WAIT），Windows 必须 `SO_EXCLUSIVEADDRUSE`
  ——Windows 的 SO_REUSEADDR 允许别人抢占在用的端口，两个实例都会"启动成功"但只有一个
  收到请求。预检和真监听共用 `bind_for_listening`，两边判断才不会打架。

## 跨平台边界（2026-10-26 定）

- 控制面：Windows / Linux / macOS 都能跑；本机 site 跟着平台走。
- **远程 site 只支持 Linux**：靠 `uname` / `ss` / `pkill` / `nohup` 管理进程。
  `ssh.probe_target` 先跑 `_posix_target()`，非 POSIX（无 `uname` 或 Git-Bash 的
  `MINGW64_NT-*`）直接 `ok=False` + `NOT_LINUX` 说明，不再跑后续命令。
- `decide_carrier` 区分 `unknown`（没探测过）与 `unsupported`（探测过、机器不行）：
  后者把 `probe.error` 原样显示出来，`ensure_ready` 直接抛这条原因。

## 视觉区分身份的设计（不用整卡颜色）

协议徽章已占颜色（MB 蓝 / S7 琥珀 / UA 紫），第二条颜色轴学不动。改用**身份 chip（自建=灰 / 连接=青）+ 虚线边框 + 副标题文字 + site 页常驻图例**，去掉颜色也读得懂。

## 工程约束

- 无 Alembic：`create_all()` 不会给旧表加列，schema 变更要手写 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 的幂等迁移（登记在 `migrate.py` 的 `EXPECTED_COLUMNS`）。
- 前端是 React + Vite + zustand，类型手写；重新构建 dist 后刷新浏览器即可（不必重启 control-plane，但 dist 必须在启动时存在）。
- 主题默认深色，`poly.theme` 存 localStorage，浅色是 `:root[data-theme='light']` 重定义 token。
- 跑 pytest 用 `.venv/Scripts/python.exe -m pytest`（仓库自带 venv）；系统 python 没装 pytest。
- **改了 `app.py` 后必须重启控制面进程**，`.venv/Scripts/python -m pytest` 加载的是旧模块。曾因此出现「磁盘上有修复、进程跑旧代码」的假 bug（停用 link 后刷新仍有数据）。`uv run` 才会用新代码。
- control-plane 的 DB 引擎在 **import 时**绑定 `POLY_DATA_DIR`，测试要用子进程 + 启动前设 env；另注意环境里的 `HTTP_PROXY`，httpx 必须 `trust_env=False`，否则请求变成绝对 URI 形式被 404。
- 控制面默认端口 **25011**（2026-10-23 从 8000 改的）；前端 `npm run dev` 的 5173 代理指向它。

## 停用/启用的语义（enabled 是期望状态，不是 start/stop 命令）

- 停用写 `PATCH /api/servers/{id}` 的 `enabled=false`：sim 停进程释放端口，link 真断开。控制面重启只恢复 `enabled=true` 的。
- `POST /restart` 对已停用实例返回 **409**——不能绕过期望状态直接起进程。
- 停用后 `/values` 返回 stopped，`/area` 与 `/write` 返回 **409**。
- **为什么必须三层防护**：`ModbusTcpClient` 在 socket 关闭时会自动重连（对活链接是必需的），所以①`_state_of` 对停用实例不能去探询（探询就会重连）②API 要先拒绝 ③`LinkSupervisor._require_active()` 在 client 前兜底。少任何一层，一次页面刷新就能把停用的 link 偷偷接回去。
- 每个实例**只有一个** `ModbusTcpClient`，缓存在 `app.state.link_supervisors`（按实例 id），删除实例时要 `pop` 掉，否则漏对象。

## 与用户的协作方式

技术选型与架构由我拍板，用户只确认业务意图（他说「我不太懂这些」）。给方案时**必须带推荐项 + 理由**，别抛一堆选项让他权衡。用户偏好一次只问一个问题、选项化提问。
