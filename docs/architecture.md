# 架构设计（v0.4）

多协议 PLC **服务端**模拟器：一个管理平台 + 每台目标服务器上一个 agent。用户在页面上创建 site（= 一台服务器），在 site 下创建若干**互相独立**的模拟器实例（Modbus TCP / S7 / OPC UA），供外部 client 连接做测试。

## 0. 当前实现状态（v0.1 本机版 + v0.2 远程部署 + v0.3 link 实例）

- 已实现：FastAPI 控制面 + React 前端 + 内嵌 Supervisor + `poly_sim_runtime` 单实例进程 + Modbus / S7 / OPC UA 三种协议 + SQLite / 本地配置与日志。
- **`host` 为空的 site 走内嵌 Supervisor；填了 `host` 的 site 走 SSH 远程部署**（`poly_control_plane/deploy.py`：推源码包 → 目标机建 venv → 每实例一个进程，控制端口只绑目标机 127.0.0.1，平台经 SSH `direct-tcpip` 通道访问）。
- **link 实例已实现**：实例分两种身份（`kind`），`sim` = 平台部署并运行；`link` = 平台作为 **client** 连一台别人正在跑的 Modbus TCP / S7 / OPC UA server，不部署、不占端口，页面读写都是真实往返。见 §2.1。
- **尚未实现**：docker driver、WebSocket 推送（现在是 1 秒轮询）；登录与两级权限见 §11。常驻 agent 与 WS 会话是**已废弃的规划**（§4 已标注），不是待办。
- 端口冲突校验按**机器**作用域：本机各 site 共用一张端口表，每个远程 site 独立（见 §2）。

## 1. 目标与非目标

**目标**：从页面动态创建/启停/改配置模拟器实例；每个实例独立端口、独立数据、独立进程；实例崩溃或配置错误不影响同机其他实例，也不影响其他 site。

**非目标（明确不做）**：

- 不做跨协议数据共享（Modbus 与 OPC UA 不共享内存，各自一份数据面；S7 / OPC UA 用标签表描述地址空间）；
- 不做实例间联动、不做 PLC 间通讯模拟；
- 不做真实 PLC 的代理/网关。

> 早期设计里的 Device / Binding / 跨协议共享镜像已废弃。理由：需求是"多个互不影响的被测 server"，共享会引入不必要的耦合与 IPC。

## 2. 层级

```
平台（管理端，跑在哪台机器都行）
└── Site = 一台目标服务器 = 一个 agent
    ├── Server 实例 A：modbus_tcp，bind 0.0.0.0:502    → 4 个原始数据区 + 独立内存（无标签表）
    ├── Server 实例 B：s7，        bind 0.0.0.0:102    → 独立标签表 + 独立内存
    └── Server 实例 C：opcua，     bind 0.0.0.0:4840   → 独立标签表 + 独立内存
```

- 平台不是 server 也不是 client；它只存配置、下发、收状态。
- 隔离分三级：机器（site）、进程（实例）、端口。
- 端口唯一性：**同一台物理机上不能重复 bind**。校验按机器分组：本机各 site 共用一张表，每个远程 site 各算一台（`_port_conflict`）。不同机器上的同名端口互不影响。
- 端口校验不止查库：创建/改端口时还会真 bind 一次（本机）或在目标机上实时 `ss -ltn`（远程，`RemoteSupervisor.port_in_use`）——库里没有记录、但被别的进程占着的端口同样要让创建失败，否则实例出生即 error。改端口才做实时检查；只改名时跳过，因为实例自己正占着那个端口。
- **同一 site 下可以有任何多个实例，同协议也可以**（2 个 Modbus、2 个 OPC UA 都是正常用法）。这是刻意的：实例的全部意义就是"几个互不影响的 server 同时被测"，而多实例在实现上本来就不共享任何东西（独立进程/内存/数据面、独立 `configs/<id>.json` 与 `logs/<id>.log`、独立控制端口；远程侧还有各自目录与 pid 文件）。**端口是唯一的硬约束**，也是唯一能把两个实例搞混的东西——因为 client 只认 `IP:端口`。历史上曾加过「每 site 每协议只能一个自建实例」的规则（`_protocol_conflict`），已删除：它挡的是合理需求，判断"用户会不会搞混"不是平台该做的事。
- 绑定必须走 `poly_core.net.bind_for_listening`：POSIX 用 `SO_REUSEADDR`（只回收 TIME_WAIT），Windows 用 `SO_EXCLUSIVEADDRUSE`（否则别的进程能抢占你在用的端口，两个实例都会"启动成功"而只有一个收到请求）。预检（`can_bind`）与真监听共用这一个函数，保证两边判断一致。
- 建议按 site 划分端口段（如 site 12 用 5020-5099），避免实例增多后人工撞端口。

### 2.1 两种实例身份：sim 与 link

| | `sim`（自建） | `link`（连接） |
|---|---|---|
| 是什么 | 平台部署并运行的 server 进程 | 平台作为 **client** 的一条 TCP 连接 |
| 占端口 | 是，bind 在目标机上 | 否，只连出去 |
| 进程 | 每实例一个，平台管理启停 | 无 |
| 配置来源 | `ServerConfig` + 标签表 | `target_host` / `target_port` / `unit_id`（Modbus）/ `rack`+`slot`（S7）/ `namespace_index`（OPC UA）/ `timeout_ms` |
| 数据面 | 自己的四个数据区或标签表 | 对方设备的数据区（Modbus）或标签表（S7 / OPC UA），读写即真实往返 |
| 支持协议 | Modbus TCP / S7 / OPC UA | Modbus TCP / S7 / OPC UA |
| 状态 | Supervisor 从 runtime 控制口拉 | 一次探测往返，不可达时退避到 5s |
| 页面上 | 「自建实例」分组 + 钢蓝身份 chip + 实线卡片 + 「部署在 <host>」 | 「连接实例」分组 + 青色身份 chip + 虚线卡片 + 「连接 → <host>:<port>」 |

设计取舍：

- **不把整卡染色**。协议徽章仍然占用协议色（MB 蓝 / S7 琥珀 / UA 紫）。身份区分首先靠 site 页的**两个分组区**（上「自建实例」、下「连接实例」），其次靠更大的身份 chip、卡片左侧 3px 身份条和副标题。自建用低饱和钢蓝，连接用青绿；钢蓝不抢 S7 的琥珀，也不带告警含义。身份色只出现在分组标题、chip、身份条和新建卡片上，不覆盖整张卡片；去掉颜色后仍有分组标题、图标和文字可读。
- **连接前必须先通**。`create_server` / `update_server` 对 link 会先真连一次，连不上就拒绝保存——避免出现一张从出生就是红的卡片；改指向同样要先验证，验证失败不影响原连接。
- **input / discrete 不可写**。真实 server 不会接受这两个区的写入（FC04 / FC02），所以连接实例的页面上这两个区直接不提供写入入口，而不是等对方报错。
- **设备应答 ≠ 连接故障**。`ModbusException`（设备应答并拒绝）和只读区拒绝都不改连接状态，只进连接历史；只有连不上/超时/掉线才判 error。否则一次被拒的写入会让一条好连接红 5 秒。
- **写入要二次确认**。连接实例的写入弹窗要求勾选「我确认写入外部设备」才能点保存——真实设备没有撤销。

### 2.2 控制面生命周期与运行时接管

**控制面不是数据面。** PLC client 直连实例端口，所以控制面退出不应该带走 runtime。现行语义：

- 正常退出（Ctrl+C / 服务 stop）只 **detach**：停监控线程、关日志句柄、把清单写入 `data/runtimes.json`，进程继续 listen。远程实例本来就是目标机上的后台进程，控制面退出不影响它们。link 实例只是控制面进程内的 client 连接，控制面退出时随之断开——对端设备不受影响，这也是「连接可以关、创建的 server 不能关」的边界。
- 下次启动按 `enabled` 收敛：本机 `Supervisor.start` 先尝试 **adopt**，远程 `RemoteHost.start_instance` 同样。清单记录 `pid`、进程创建时间、控制端口、配置指纹、启动时间；adopt 的条件是「pid 仍属于原进程（创建时间匹配）+ 该控制端口的 `/state` 返回同一个 `server_id` 和相同 `config_hash` + 状态 running/starting」。任一不满足就停掉旧进程、按当前配置重建；`enabled=false` / 已删除 / 已改到远程 site 的本地遗留进程由 `reap_unwanted` 清掉。
- 因此「控制面重启」保留进程、内存数据、`started_at`；「显式 restart」仍然是重建，数据清零。**整机重启**是另一回事：进程没了，控制面会拉起 `enabled=true` 的实例，但内存数据会按空数据区/标签初值重建（不做快照）。
- 想真的停掉本机运行时：`PATCH /api/servers/{id}` 的 `enabled=false`（单个），或启动控制面时加 `--stop-on-exit` / `POLY_STOP_RUNTIMES_ON_EXIT=1`（退出时停掉本机运行时；远程实例仍在各自机器上）。默认不加就是保留。
- POSIX 上 runtime 以 `start_new_session=True` 启动，终端 Ctrl+C 只发给控制面进程组，不会顺带杀掉 runtime。Windows 用 `CREATE_NO_WINDOW`，并额外尝试 `CREATE_BREAKAWAY_FROM_JOB`：IDE / 服务管理器的 job object 默认会连整个进程树一起收走，breakaway 是让 runtime 活过控制面异常退出的关键；job 不允许 breakaway 时退回普通 `CREATE_NO_WINDOW`。另外，uv 在 Windows 的 venv `python.exe` 是个 launcher（真正解释器是它的子进程），所以 Supervisor 直接用 `sys._base_executable` + venv site-packages 启动 runtime，保证 `Popen.pid == /state.pid`，否则 kill/adopt 会认错进程。
- 用 systemd / Windows 服务托管控制面时，服务管理器不能连整个进程树一起杀：systemd 用 `KillMode=process`，NSSM 关闭 kill-process-tree，否则代码里的 detach 无效。
- `data/runtimes.json` 是控制面记住「哪些进程是我的」的唯一凭据；删 `data/` 前先确认没有遗留 runtime 在跑。

## 3. 物理事实：端口只能由本机进程监听

目标机是真实服务器（如 `10.0.0.12`、`10.0.0.15`），客户端分别连这些 IP。

> 要让 `10.0.0.12:502` 可连，**必须有一个进程运行在 10.0.0.12 上并 listen(502)**。平台在任何地方都无法替另一台机器创建 socket。

因此每台目标服务器装一个 agent（一次性动作）。平台可以与其中之一同机，也可以完全独立部署。

agent 不是 client，也不是平台的复制品：它不存配置（配置在平台）、不转发协议流量（PLC client 直连端口）、不含业务逻辑，只做"在本机起停实例 + 回报状态/日志"。

**平台边界：远程部署只对 Linux 做，Windows 明确排除。** Windows 机器不通过远程部署接入——它自己跑控制面，site 的 host 留空即本机模式（实例仍在那台机器上起进程，只是中间没有 SSH 这一层）。这是本节那条物理事实的延伸：要在远端管进程，就必须依赖 `uname` / `ss` / `pkill` / `nohup` 这套 POSIX 工具。其他 POSIX 系统（macOS 等）同样拒绝——`ss` / `pkill` 的行为并不一致，半通不通比明确拒绝难查得多。拒绝发生在探测的第一步（`ssh._platform_verdict`），不是等到部署失败。

## 4. Bootstrap 与实例生命周期

> **本节保留的是已废弃的规划（WS agent + 注册 token），不要再按它实现。** 现行模型（P2 定的）：远程侧不装常驻进程，平台按需 SSH 上去推源码、建 venv、起停进程；本机侧由内嵌 `Supervisor` 管进程。两种都靠磁盘上的配置文件而不是注册会话。
>
> 现行生命周期的实际形状：创建实例 → 立刻部署并启动（远程首次会先做一次性准备）→ 之后平台每次按需查询状态/日志；`enabled` 是期望状态，控制面重启时只把 `enabled=true` 的实例拉回来。

（以下为历史规划，仅作背景：）

1. 平台生成一次性注册 token。
2. 目标机执行 `plc-agent --platform http://<platform>:25011 --token <t>`（或 compose / 服务方式）。
3. agent 上线为"待认领"，上报 IP / 主机名 / OS / 版本 / 网卡 / **支持的协议**。
4. 用户命名为 Site。
5. 在该 Site 下创建实例：协议 + bind_ip + 端口 + 数据面配置（S7 / OPC UA 的标签表）；agent 收到期望配置后启动进程并回报 `listening / port_in_use / crashed / unsupported`。

**收敛而非命令式**：平台存 desired + `config_version`，agent diff 后自行启停重建。这样 agent 重启、断网重连都能自愈，不需要 `start/stop` 接口。

**断网要继续跑**：last-known-good 配置落本地磁盘，恢复连接后先对齐版本再动作。

**健康判定**：进程存活 + 本机 TCP connect 目标端口成功，才算 `listening`；只看进程存活会误报（例如端口 bind 失败但进程还在）。

## 5. 实例的启动方式（Launcher）

一个实例 = 一个 OS 进程（native）或一个容器（docker）。抽象成 `Launcher` 接口，两种实现：

| driver | 启动 | 适用 | 成本/代价 |
| --- | --- | --- | --- |
| `native`（默认，已实现） | `python -m poly_sim_runtime --config data/configs/<id>.json --control-port <p>`，Supervisor 重定向 stdout 到 `logs/<id>.log` | 有 Python 的目标机 | 需要目标机装依赖 |
| `docker`（规划中） | `docker run --rm --name sim-<id> -p <port>:<port> <image>` | 想要环境隔离/统一镜像 | 镜像构建分发；启动 1~3s；每实例约 100MB |

日志与 driver 无关：统一落 `data/logs/<server_id>.log`，按追加写入，UI 只 tail 最后 N 行。

不要"每个实例生成一个 docker-compose 项目"：动态增删场景下 compose 会留下 N 个项目和卷命名问题，直接用 `docker run`/Docker API 更简单。

## 6. 端口映射的真实语义

`-p 4841:4840` 的本质是**宿主机 listen 4841** 再转发进容器。因此：

- 宿主机端口是**真实占用**的，其他进程不能再 bind；
- client 连的仍是 `<宿主机IP>:4841`，不知道容器存在；
- 非标端口可行，但必须让 client 知道：OPC UA 的 `endpoint_url` 要写 `opc.tcp://<ip>:4841`；S7 要求 client 支持自定义端口（部分 SCADA 固定 102 则不可用）。

## 7. 协议能力矩阵

agent 上报 `{os, arch, capabilities}`，平台据此决定可创建哪些协议。三层校验：UI 置灰 → API 拒绝 → agent 自身拒绝。

| 协议 | Windows | Linux | 备注 |
| --- | --- | --- | --- |
| Modbus TCP | ✅ | ✅ | 502 在容器内默认 root 可绑；非标端口见 §6 |
| OPC UA | ✅ | ✅ | 必须显式配 `endpoint_url`（留空时自动用局域网 IP） |
| S7 (snap7) | ✅ | ✅ | `python-snap7 >= 3.3` 的 server 是纯 Python 实现，不依赖 `snap7.dll`；capability 仍按实测上报 |
| native driver | ✅ | ✅ | 需 Python + 依赖（`uv sync`） |
| docker driver | ✅ | ✅ | 规划中，尚未实现 |

能力用数据表达而不是硬编码 `if`：将来在 Linux 放开 S7，只改能力表。

## 8. 自研 vs 现成模拟器

结论：**协议引擎用现成库，实例管理层自研**。现成模拟器都是 GUI 工具或静态配置容器，无法满足"按需创建 N 个实例 + 下发标签 + 回传状态/日志"，硬套反而更贵。

| 协议 | 现成方案 | 为什么不够 |
| --- | --- | --- |
| Modbus | diagslave、ModbusPal、各类 `modbus-server` 镜像、pymodbus 示例 | 静态寄存器表 / GUI 手工点；改配置要重启；无实例生命周期 API；镜像需拉取 |
| S7 | snap7 demo server、NetToPLCSim、PLCSIM Advanced | demo 是固定 DB 区的 exe；NetToPLCSim 是转发器；PLCSIM 收费 |
| OPC UA | Prosys Simulation Server、open62541 示例 | GUI 配节点或代码写死；没有"按标签表动态生成地址空间"的接口 |

自研范围（薄封装，约数百行/协议）：

- Modbus TCP：手写 TCP 帧（FC 1/2/3/4/5/6/15/16），**没有标签表**——实例就是四个原始数据区，地址含义完全交给连上来的 client；服务端也不生成任何仿真值。不依赖 `pymodbus` 的 datastore（3.x 接口变动大）。四个区按完整 16 位地址空间（0–65535）固定分配，避免测试工具扫描固定范围时收到 illegal-address。
- S7：`python-snap7 >= 3.3` 纯 Python server，标签表 → DB/M/I/Q 字节区，rack/slot 可配。
- OPC UA：`asyncua` server，按标签表动态生成变量节点，`endpoint_url` 可配；变量名即 NodeId（字符串），落在一个专用命名空间里（实测索引 2：0 是 OPC UA 标准、1 是 asyncua 自己的应用命名空间）。link 侧反过来，需要对方给的那份地址，所以 `TagDef.node_id` 只在 link 上有意义——留空时按 `ns=<namespace_index>;s=<name>` 推导，正好对应我们自己 server 的命名方式。

## 9. 粗估成本（单人，含调试）

| 模块 | 估时 |
| --- | --- |
| `plc-core`：标签表、编解码、仿真值 | 3~5 天 |
| Modbus 适配器 | 2~3 天 |
| OPC UA 适配器（动态地址空间） | 4~6 天 |
| S7 适配器（面积映射 + dll 打包） | 3~5 天 |
| agent：会话、收敛、进程监督、日志 | 5~8 天 |
| 平台 API + 前端 | 10~15 天 |
| **MVP（单协议 Modbus 端到端）** | **约 2 周** |

备选路径（用现成模拟器镜像）：表面省掉协议适配器，但要补三套配置生成器 + 三套日志/状态适配 + 镜像分发，且实例化能力仍缺；综合不低于自研，还失去可控性。

## 10. 数据模型

```
site     (id, name, description, created_at)
server   (id, site_id, name, protocol, bind_ip, port, runtime, options jsonb,
          enabled, config_version, created_at)
tag      (id, server_id, position, name, data_type, area, address, bit,
          db_number, word_order, writable, description, sim jsonb)
audit    (id, created_at, actor, action, target, detail)
```

运行态（监听状态、连接数、当前值）**不落库**：Supervisor 每秒从 runtime 的 `/state` 拉取，API 读取时返回。配置版本 `config_version` 每次改标签/端口自增，Supervisor 用它判断是否重建进程。

`server.runtime` ∈ `native | docker`；`options` 按协议不同（S7 的 rack/slot、OPC UA 的 endpoint_url 与 advertise_host）。

多机形态还会加：`site.agent_token_hash / os / arch / capabilities / last_seen_at` 与 `agent` 会话表；当前本机版没有这些字段。

## 11. API 形状（已实现）

```
POST   /api/login                        Cookie 登录
POST   /api/logout                       退出登录
GET    /api/me                          当前用户
GET    /api/users                       管理员：用户列表
POST   /api/users                       管理员：创建用户（默认密码 123456）
DELETE /api/users/{id}                  管理员：删除用户
POST   /api/users/{id}/password         本人或管理员：修改密码，旧 Cookie 失效
GET    /api/health
GET    /api/system                        OS/网卡/协议能力/已占用端口
GET    /api/sites
POST   /api/sites
PATCH  /api/sites/{id}
DELETE /api/sites/{id}                    级联删除实例与标签
GET    /api/sites/{id}/servers
POST   /api/sites/{id}/servers            创建 -> 校验端口(含实时占用)/能力 -> 立即启动
GET    /api/sites/{id}/port-check         端口预检（?port=&bind_ip=&exclude=），只回答不落库
POST   /api/sites/{id}/link-test          探测「连接别人 server」的目标（?protocol=modbus_tcp|s7|opcua，不落库；OPC UA 额外返回对端 namespaces）
GET    /api/servers/{id}
PATCH  /api/servers/{id}                  改名称/端口/参数/enabled（version++ -> 重建或停用）
DELETE /api/servers/{id}
POST   /api/servers/{id}/restart          显式重启（唯一允许的命令式操作；停用实例返回 409）
PUT    /api/servers/{id}/tags             整表替换 -> version++ -> 重建
GET    /api/servers/{id}/values           状态 + 当前值 + 连接数
GET    /api/servers/{id}/area             ?area=holding&address=0&count=50 原始数据区（绕过标签表）
POST   /api/servers/{id}/write            {tag, value}
GET    /api/servers/{id}/logs?lines=N
GET    /api/audit
```

Modbus 实例**没有标签**：创建时传 `tags` 会被 API 拒绝，`PUT /tags` 同样拒绝，`sample_tags` 对它无效——它的数据面就是全 0 起步的四个区。S7 / OPC UA 才有标签表：显式给就用给的，没给且 `sample_tags=true` 生成示例标签，默认 `sample_tags=false` 创建空表。

`GET /api/servers/{id}/area` 的 `address` 是 0 基偏移（0–65535），`count` 上限 1000；前端把引用写法换算成偏移后调用（`apps/web/src/modbusAddress.ts`：`40001` 与 `400100`/`400001` 同样处理，5 位与 6 位是同一个地址的两种写法）。数据区每页 50 个，只有 `count > 50` 才分页，且只请求当前页的地址。单元格双击弹窗写入时可选 BIN / DEC / HEX 输入进制；`input` / `discrete` 对 client 只读，但控制台可以预置。

因为 Modbus 不带标签，Modbus 实例页只有「数据区」+ 一个 tab：自建实例是「日志」，link 实例是「连接」（轮询历史、往返延迟、错误原因）。S7 / OPC UA 实例则是「实时变量」+「标签表」+ 同一个第二 tab；S7 / OPC UA 的 link 没有原始数据区可看（`/area` 会以设备错误拒绝），变量表就是它的全部视图。OPC UA link 的标签表比别的协议多一列 NodeId（留空即推导），因为对方的命名空间索引和节点名都没法猜。

**link 实例的接口差异**：`GET /api/servers/{id}/logs` 返回体多一个 `link: true`，内容是连接历史而不是 runtime 日志；`GET /api/servers/{id}/values` 的 `runtime` 里 `latency_ms` 是最近一次往返耗时；`PATCH /api/servers/{id}` 传 `target_host` / `target_port` 会先探测目标，成功才改指向（失败保持原样），传 `enabled=false` 是真断开（不是暂停一个不存在的本地进程）。创建 link 实例时 `port` 必须等于 `target_port`，跳过本机端口冲突检查。

**停用不是命令式 stop**：页面上的「停用 / 启用」写的是 `PATCH /api/servers/{id}` 的 `enabled` 期望状态。`enabled=false` 时 sim 进程停止、端口释放，link 断开；控制面重启只恢复 `enabled=true` 的实例，已经在跑且配置没变的实例会被接管而不是重建（§2.2）。已停用实例调用 `POST /restart` 返回 409，必须先启用，避免绕过期望状态。停用后的 `/values` 返回 stopped，`/area` 与 `/write` 返回 409；`LinkSupervisor.read_area` / `write_area` 也会拒绝，因为 `ModbusTcpClient` 在 socket 关闭后会自动重连，不能让它被一次“刷新”重新拉起来。

**写入被拒 ≠ 连接失败**：对 link 实例写 `input` / `discrete` 会返回 400 并在请求到达网线之前被挡下（client 侧守卫），连接状态保持 running；设备自己回了 Modbus 异常码时同理——设备有响应本身就是活着的证明。只有链路级错误（连不上、超时、对端断开）才把连接标记为失败。

界面默认跟随系统主题（系统深色则深色，否则浅色），切换按钮在左侧栏底部，选择落在 localStorage `poly.theme`。

前端目前用 1–4 秒的 HTTP 轮询（页面隐藏时暂停）；Modbus「数据区」1 秒刷新。WS 实时推送是后续优化项，不是本机版的依赖。

## 12. 仓库结构

```
apps/control-plane/       FastAPI + DB：site/server/tag 配置、agent 会话、页面 API
apps/web/                 React + Vite + TS
apps/agent/               plc-agent：会话、收敛、Launcher(native|docker)、日志、状态回报
apps/sim-runtime/         单实例运行时：读自己的 config，起一个协议 server（可独立成容器）
packages/plc-core/        标签表、编解码、仿真值（协议无关，必须可单测）
packages/contracts/       平台 ⇄ agent 的 Pydantic 契约，两侧共用同一版本
packages/proto-{modbus,s7,opcua}/
deploy/                   docker-compose（平台 / agent / runtime 镜像）
docs/
```

## 13. 落地顺序

1. ✅ `plc-core` + Modbus：一个实例、四个原始数据区，能读写（Modbus 不使用标签表）。
2. ✅ `sim-runtime` 独立进程 + Supervisor 拉起/停止/日志/健康检查（native）。
3. ✅ 平台 + 前端：site/实例 CRUD、状态、实时值、标签编辑、日志。
4. ✅ 多个 site（本机）验证隔离与 capability 校验；跨机器验证待远程 agent。
5. ⏳ docker driver。
6. ✅ S7（python-snap7 纯 Python，跨平台）。
7. ✅ OPC UA（endpoint_url、地址空间生成）；证书/加密待补。
8. ✅ link 实例：平台作为 client 连接别人正在跑的 Modbus TCP / S7 / OPC UA server，页面可建可改（§2.1）。
9. ⏳ 配置回滚（audit 写入已实现）。

## 14. 已知风险

- S7 并发连接数有限，且面积映射以字节为单位，标签重叠必须校验。
- OPC UA 的 `endpoint_url` 与实际监听不一致是最常见的"连不上"原因。
- 大量实例时端口规划与防火墙需要提前约定（按 site 分段）。
- 每实例一个 Python 进程约 60~100MB 常驻内存；一台机器上跑 20+ 实例时要估算内存，必要时再考虑"一进程多实例 + 逻辑隔离"（会牺牲崩溃隔离）。
- 控制面正常退出 / 被硬杀都不会再停掉 runtime：启动时按 `data/runtimes.json` 先尝试接管，只有配置变了、进程死了、或实例已停用/删除才停掉重建（§2.2）。整机重启后 runtime 必然重新拉起，内存数据不保留。
- Modbus 的 float 字序（AB-CD / CD-AB）完全由 client 侧解释，服务端每个格子就是一个 16 位寄存器；S7 固定大端。
- **link 实例的写入打到真实设备，没有撤销**。前端有二次确认 + 只读区禁用，API 层不做额外保护（这是"连接别人的 server"的应有语义）；对端不可达时页面会红，但断开瞬间的写入会静默丢失——需要可靠写入的场景应在上位机侧做。
