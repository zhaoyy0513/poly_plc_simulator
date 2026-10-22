# 远程 site 与 server / client 双身份设计

日期：2026-10-22
状态：P1 已上线、P2 已实现并在真实机器（10.102.129.45）验证；P3 起待做
范围：v0.2 本机版 → 远程部署 + 连接别人的 server

## 1. 要解决什么

现状三个问题：

1. **site 没有真实对应物**，只是一个文件夹，用来给人分组，不承担任何机器信息。
2. **实例只能在本机创建**。想让 `10.102.129.12:502` 上有东西，只能手动去那台机器上装东西。
3. **不能连别人已经跑着的 server**。想看/改 18 环境那台机器的 502，只能另开工具。

目标形态：

- site = **一台真实服务器**，site 里存它的访问信息。
- site 下创建 `sim` 实例 = **平台把运行时推到那台机器上运行**，端口开在目标机上。
- site 下还能创建 `link` 实例 = **平台自己作为 client 连到别人的 server**，页面上的查看/编辑体验和自建实例完全一致（含 BIN / DEC / HEX）。
- 实例**必须标明身份**：`sim`（我们起的 server）还是 `link`（我们连的 client）。

非目标：不做登录与权限；不做本地端口代理；不做 S7 的 client 连接；不做 docker 载体（只留口子）。

## 2. 已确认的决策

| 议题 | 结论 | 理由 |
| --- | --- | --- |
| 部署形态 | 统一平台 + SSH 推送，**目标机不常驻 agent** | 单人多环境，运维成本最低；agent 的自愈/收敛留到真需要时 |
| 连接形态 | **平台直连**（页面读写直接作用于远程） | 就是「查看和编辑」本身，零端口成本；数据源抽象已预留代理能力 |
| site 语义 | **site = 一台机器**；`host` 为空表示「本机」 | 兼容现有 `local` site，不用迁移数据，调试时不必先备远程机 |
| 使用范围 | 单人多环境，无登录 | 登录/授权是正交一层，后加不推倒 |
| SSH 认证 | 账号密码，端口可配（默认 22）；**不明文展示**，DB 里也加密 | 防 `app.db` 被拷走；防不住能读本机文件的人 |
| link 协议 | 先 **Modbus + OPC UA** | S7 client 在 Windows 平台机上要 `snap7.dll`，后置 |
| 部署载体 | **native 进程**；Python 太旧时用 **uv 装一个独立 CPython**；docker 只留口子 | 见 §5.1：docker 在共享机器上通常没权限，uv 无 root、30 秒、清理就是 `rm -rf` |
| 远程进程管理 | **纯 SSH 按需查询**，SSH 连接按 site 复用 | 目标机零常驻组件；1 秒轮询复用连接成本可接受 |

## 3. 数据模型

### site（现有表，加列）

```
host            str   空 = 本机（走内嵌 Supervisor）
ssh_port        int   默认 22
ssh_user        str
ssh_secret      str   密码密文，API 与 UI 永不返回明文
deploy_dir      str   默认 ~/.poly-sim
python_bin      str   探测出来的目标机 Python（如 /usr/bin/python3.11）
last_probe_at   str   上次探测时间
last_probe      json  {os, arch, python, writable, ports_in_use[]}
```

### server（现有表，加列 → 概念上改叫「实例」）

```
kind            str   'sim'  |  'link'     默认 'sim'
target_host     str   link 专用：要连的地址
target_port     int   link 专用：要连的端口
target_options  json  link 专用：unit_id / endpoint_url / 命名空间 / 超时
```

`sim` 沿用现有 `protocol / bind_ip / port / options / tags`；`link` 只用 `protocol + target_*`，`bind_ip/port` 留空（平台不监听任何端口）。

### 迁移方式

项目没接 Alembic，`SQLModel.metadata.create_all()` **不会给已存在的表加列**。需要写一个轻量迁移：启动时 `PRAGMA table_info(site)` 检查缺哪些列，缺就 `ALTER TABLE ADD COLUMN`，幂等，与现有 `_purge_modbus_tags` 一样放在 lifespan 里。

### 运行态仍然不落库

状态、连接数、日志都从数据源实时取，和现在一致。

## 4. 数据源抽象（核心）

统一接口，页面完全无感：

```python
class DataSource:
    def health(self) -> StateInfo          # 存活 / 客户端数 / 错误
    def read_area(self, area, address, count) -> list
    def write_area(self, area, address, value) -> None
    def read_snapshot(self) -> dict        # S7 / OPC UA 的具名值
    def write_named(self, name, value) -> None
    def logs(self, lines) -> str
```

| 实现 | 用于 | 底层 |
| --- | --- | --- |
| `LocalRuntimeSource` | 本机 site 的 sim | 现有 Supervisor + runtime HTTP（不动） |
| `SshRuntimeSource` | 远程 site 的 sim | SSH 连接复用 + `direct-tcpip` channel 打到目标机 `127.0.0.1:<控制端口>` |
| `ModbusClientSource` | modbus link | 平台本地 pymodbus client |
| `OpcUaClientSource` | opcua link | 平台本地 asyncua client |

关键点：远程 runtime 的**控制端口仍然只绑目标机 127.0.0.1**，平台通过 SSH channel 打过去，目标机不会额外开网络端口，安全模型跟现在一致。

现有 API 不用新增数据面端点：`GET/POST /api/servers/{id}/area` 直接换数据源即可复用。

## 5. sim 实例的远程部署流程

目标机目录布局（每个 site 一个共享 venv，不要每实例一个）：

```
~/.poly-sim/
├── app/                          # 上传的源码包（含 requirements*.txt、VERSION）
├── venv/                         # 首次部署时建，之后复用
├── .version / .installed         # 内容哈希，决定是否要重新上传 / 重装
└── instances/<instance_id>/
    ├── config.json               # 与本机版完全同一份格式
    ├── control.port              # 控制端口，供 SSH channel 重连
    ├── runtime.pid
    └── runtime.log
```

步骤：

1. **创建 site 时探测**：SSH 连一次，取 `uname`、发行版、Python 版本、`deploy_dir` 可写性、**本 SSH 用户能否真的驱动 docker（`docker info`）**、已占用端口列表。探测结果入库，UI 上直接显示「这台机器能不能用」。
2. **创建实例**：SSH 上写 config JSON，`( PYTHONUNBUFFERED=1 exec venv/bin/python -m poly_sim_runtime --config ... --control-port <p> ) >> log 2>&1 < /dev/null & echo $! > runtime.pid`。`exec` 不能省，否则 `$!` 是外层 shell 的 pid，kill 掉它运行时会变成孤儿继续占端口。
3. **依赖安装**：内容哈希变了才做（`--force-reinstall`，因为版本号永远是 0.1.0，pip 否则会认为“已满足”而留着上周的代码）。协议适配器（python-snap7 / asyncua）单独一趟且允许失败：装不上不该拖垮 Modbus。
4. **健康检查**：远程 `kill -0 <pid>` **并且**从平台侧经 SSH channel 请求 `/state` 成功才算 running。
5. **停止 / 删除 / 清理**：按 config 路径 `pkill -f`（pid 文件丢了、机器重启过也能停干净）→ 删除则 `rm -rf` 该实例目录；site 级清理 = 停全部 + `rm -rf deploy_dir`。
6. **日志**：远程 `tail -n`，走同一个 SSH 连接。日志不落平台机。

### 5.1 载体决策（机器决定，UI 只告知）

平台探测完只**告诉**用户会用哪种方式，不提供选择；不满意就去修机器（装 Python / 开网络），然后关掉弹窗重来。

| 顺序 | carrier | 条件 | 做法 |
| --- | --- | --- | --- |
| 1 | `native` | 目标机有 Python ≥ 3.10（含 `python3.11` 这类版本化命令） | 直接 `python -m venv` |
| 2 | `uv` | Python 太旧/缺失 | `curl astral.sh/uv` → `uv python install 3.11` → 建 venv。无 root、约 30s、只写 `~/.local` 与部署目录 |
| 3 | `docker` | 无 Python 但 `docker info` 可用 | **未实现**，只报“请先装 Python 或开放网络” |
| 4 | `unsupported` | 都不满足 | 原样告诉用户缺什么 |

为什么最后才是 docker：真实机器（CentOS 8、`prime` 用户）上 docker 二进制人人可见，但 `docker.sock` 只有 root/docker 组能用；uv 的独立 CPython 反而更简单、更好清理，也不依赖守护进程。


## 6. link 实例（连别人的 server）

**Modbus link**：平台本地起 pymodbus client 连 `target_host:target_port`。数据区读写映射到 FC 1/2/3/4/5/6/15/16。参数：unit_id（默认 1）、超时（默认 3s）、失败重试。
- 安全阀：**写别人的设备要二次确认**。UI 在写入弹窗上标明「这是远程设备 10.x.x.x:502，写入会直接改动对端数据」，并要求勾选确认。只读区（input / discrete）在真实 server 上写不了，UI 给出提示而不是报错。

**OPC UA link**：asyncua Client 连 `endpoint_url`。注意**页面形态不同**：OPC UA 没有寄存器网格，要做成「节点浏览器」——连接后 browse `Objects`，列出变量节点，点开读值 / 写值。具名读写与 S7 一侧的 `read_snapshot` 复用同一条路径。

**连接状态**：轮询失败要给出具体原因（连接被拒 / 超时 / UA endpoint 不匹配 / 证书不受信），而不是笼统的「失败」。平台页面隐藏时暂停轮询（现有机制已实现）。

## 7. UI 改动

- **Site 表单**：新增 host、SSH 端口、用户名、密码（保存后再打开只显示「已配置，点击修改」），一个「测试连接」按钮回显探测结果。
- **实例卡片**：加 `server` / `client` 徽标。sim 显示「部署在 10.x.x.x（~/.poly-sim）」，link 显示「连接到 10.x.x.x:502」。
- **实例页**：Modbus 的 sim 与 link 共用现有「数据区」页（双击/BIN-DEC-HEX 全都不变）；OPC UA link 用节点浏览器；sim 有「日志」（远程 tail），link 没有日志页，改为一条简短的连接事件信息。
- 主题、tab 规则、现有交互全部不变。

## 8. 风险与取舍

- 密码加密只防「数据库被拷走」，防不住能读本机文件的人。单人多环境场景够用。
- 目标机必须能装 Python 依赖；离线场景需要打包方案（第二阶段）。
- 每实例一个 Python 进程约 60–100MB，远程机器实例数量要先算内存。
- 端口冲突：平台侧校验不到远程机器的占用，必须在部署前用 SSH 探测；同一台目标机上的实例端口不可重复。
- 1 秒轮询 × N 实例会放大远程调用；按 site 复用连接，且页面隐藏时暂停。
- 没有登录，不要暴露到公网（现有说明继续有效）。

## 9. 分阶段实施

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| P1 | `git init`；site 表改造 + 轻量迁移；密码加密；探测与「测试连接」API；site 表单 UI | site 能存访问信息并验证连通，实例行为不变 |
| P2 | SSH 部署模块 + 远程 Supervisor + sim 实例远程生命周期（创建 / 启停 / 删除 / 日志 / 健康） | 在真实远程机上起起停停跑通（**已完成**，见 §11） |
| P3 | DataSource 抽象落到 API + Modbus link（pymodbus client） | 能连 18 环境的 502 看/改数据 |
| P4 | OPC UA link（asyncua client + 节点浏览器） | 能连 OPC UA server 浏览与读写 |
| P5 | 运维补齐：site 清理、端口占用探测、依赖缓存、离线打包 | 日常好用 |

## 10. 测试策略

- **SSH 层不连真机器测**：把 `SshRunner` 定义成协议（接口），测试注入一个记录命令的 FakeRunner，断言命令序列与目录布局。
- **link 数据源可以真端到端**：平台本地先起一个 Modbus sim 实例当「别人的 server」，再用 `ModbusClientSource` 连它读写 —— 真实协议、不需要真 PLC。OPC UA 同理。
- 现有 15 个 pytest 必须保持全绿；新增用例按阶段补。P2 补了 `tests/test_deploy.py`（载体决策、依赖分组、bundle 稳定性）。

## 11. P2 实施记录（2026-10-22）

在 `10.102.129.45`（CentOS Linux 8，系统 Python 3.6.8，`prime` 用户）上验证通过：

- 首次部署约 80 秒（装 uv + CPython 3.11.17 + venv + 依赖），之后每个实例启动约 1 秒。
- 平台写入 holding[0]=4660 → 从开发机用真实 Modbus 客户端读 `10.102.129.45:15020` 得到 `[4660, 0, 0, 0, 0, 0]`。
- 重启 control-plane 后远程实例被补偿启动并正确报 running；停用 / 启用 / 重启 / 删除全部通过，删除后目标机进程、端口、实例目录均清理干净。
- 目标机占用：`~/.poly-sim` 96MB + `~/.local/share/uv` 95MB。

踩到的三个坑，已写进代码注释：

1. **paramiko 通道不能用 EOF 判定命令结束**。命令留下后台进程时 channel 不关，`stdout.read()` 一直阻塞到超时。改为把退出码内联到 stdout（`__poly_rc=`），读到标记就停。
2. **SFTP 不展开 `~`**。部署目录必须在远端 `cd ... && pwd` 解析成绝对路径，否则上传报 `FileNotFoundError`。
3. **`&` 后的 `$!` 不一定是目标进程**。`cd X && nohup ... &` 会先 fork 一个子 shell，kill 它不会带走运行时；必须 `( exec ... ) &`。

发现并已提醒用户的安全问题：目标机 **2375 端口（Docker API）无认证监听**，与部署无关，建议关闭。

