# Poly PLC Simulator

多协议 PLC **服务端**模拟器。在浏览器里创建 site（一台被测服务器），在 site 下创建互不影响的
Modbus TCP / S7 / OPC UA 实例；真实的 PLC 客户端（SCADA、PLC、脚本）直连实例端口即可读写。

本机版（v0.1）已经可以完整跑通：每个实例是独立 OS 进程、独立端口、独立标签表和内存，
数据面就是协议本身（寄存器 / 字节区 / UA 节点），页面上的实时值与客户端看到的是同一份数据。

```
平台（FastAPI + React）
└── Site = 一台目标服务器 = 本机 Supervisor
    ├── 实例 A：modbus_tcp  0.0.0.0:15020  → 自己的标签表 + 寄存器
    ├── 实例 B：s7          0.0.0.0:15102  → 自己的标签表 + DB/M/I/Q
    └── 实例 C：opcua       0.0.0.0:15400  → 自己的标签表 + 地址空间
```

> 本机版把“目标服务器”折叠成了当前这台机器：所有 site 共用这台机器和端口空间，
> control-plane 内置 Supervisor 直接拉起 runtime。远程 agent、WebSocket 会话和 docker
> driver 是后续项，见 [docs/architecture.md](docs/architecture.md)。

## 快速开始

环境：Windows / Linux / macOS，Python 3.13（用 `uv` 管理），Node.js 20+（前端构建需要）。
本机已验证：Windows 11 + Python 3.13.14 + Node 24。

### 1. 安装 Python 依赖

```powershell
uv sync --group dev
```

### 2. 启动（二选一）

#### 方式 A：单端口（推荐给日常使用）

```powershell
cd apps/web
npm install
npm run build
cd ../..

uv run python -m poly_control_plane --host 0.0.0.0 --port 8000
```

打开 `http://<本机局域网IP>:8000/`，例如 `http://10.102.128.181:8000/`。
API 也在同一端口：`http://<IP>:8000/api/...`。

> control-plane 启动时只检查一次 `apps/web/dist` 是否存在（不存在就不托管页面）。之后重新构建
> **不需要**重启：产物文件名带 hash，浏览器刷新即可；如果看着还是旧界面，按 Ctrl+F5 强制刷新。

#### 方式 B：前后端分开（开发模式，热更新）

终端 1 —— 后端：

```powershell
uv run python -m poly_control_plane --host 0.0.0.0 --port 8000
```

终端 2 —— 前端：

```powershell
cd apps/web
npm install
npm run dev
```

前端监听 `0.0.0.0:5173`，并把 `/api` 代理到 `127.0.0.1:8000`。
打开 `http://<本机局域网IP>:5173/`。

### 3. 局域网 / 其他设备访问

三个服务都绑 `0.0.0.0`，直接用本机 IPv4 访问即可：

- 页面：`http://10.102.128.181:8000`（方式 A）或 `http://10.102.128.181:5173`（方式 B）
- 协议端口：客户端连 `10.102.128.181:15020` 等，不经过页面

如果其他机器连不上，检查 Windows 防火墙是否放行了 8000 / 5173 以及实例端口。
监听地址决定谁能连：`0.0.0.0` = 所有网卡（本机 `127.0.0.1` 和局域网 IP 都行）；
选具体网卡（如 `10.102.128.181`）时**只有该地址能连**，本机用 `127.0.0.1` 会超时。
实例页会给出提示，并提供「改为 0.0.0.0」按钮。

## 使用流程

1. 首页「新建 Site」——例如 `12 服务器`。
2. 进入 Site →「新建实例」——选择协议、监听地址、端口（会自动建议一个空闲端口）。
3. 实例创建后立即启动。
   - **Modbus 实例没有标签，也没有仿真值**：只有 01/02/03/04 四个原始数据区，所有地址初始为 0，
     等 client（或者你在页面上）写进去才会变。
   - S7 / OPC UA 实例默认创建**空标签表**（全是 0）；只有勾选「创建示例变量」才会生成
     `temperature`（正弦）、`pressure`（随机）、`counter`（斜坡）、`running`（翻转）这 4 个演示变量。
4. 打开实例页：
   - **Modbus 实例只有两个标签页：「数据区」和「日志」**（它按标签看没有意义，数据区就是唯一视图）。
     - **数据区**（默认打开）：01/02/03/04 切换功能码，设置起始地址、长度；起始地址支持 0 基偏移
       （如 `100`）或引用写法（如 `400100`），默认 FC3、长度 50。数据按多列网格展示，每页 50 个，
       超过 50 才分页；右上角的 BIN / DEC / HEX 切换的是**显示**进制。
       - **双击单元格（或聚焦后回车 / 空格）弹出写入框**：数值可按 BIN / DEC / HEX 三种**输入**进制填写，
         寄存器 0–65535，线圈 / 离散输入只能 0 / 1。保存立即生效、不重启实例，已连接的 client 马上能读到。
       - 左侧栏可折叠成图标栏，给数据区腾出更多列。
     - **日志**：runtime 的 stdout/stderr。
   - S7 / OPC UA 实例是另外三个标签页：**实时变量**（每秒刷新、可直接写值）、
     **标签表**（增删改变量、类型、地址、字序、是否可写、仿真模式）、**日志**。
5. 用真实客户端连接卡片上显示的地址。监听地址为 `0.0.0.0` 时，本机客户端也可用卡片下方的
   `127.0.0.1:<port>`。
6. 界面默认是深色主题，左侧栏底部的月亮 / 太阳按钮可切换到浅色，选择会被记住。

> Modbus 的数据区**不做任何仿真**：服务端不会自己动这些值，页面写入和 client 写入是同一份数据。

保存标签表会重建数据面并重启该实例（约 1–3 秒），已连接的客户端需要重连。这只影响 S7 / OPC UA——
Modbus 没有标签表，数据区写入是即时的，不会重启。

## 协议说明

创建实例后，卡片的连接地址就是客户端要用的地址。默认端口从 15020 / 15102 / 15400 开始，
避开 Windows 保留端口段。

### Modbus TCP

- 手写标准帧，支持 FC 1 / 2 / 3 / 4 / 5 / 6 / 15 / 16。
- unit id 默认 1；地址是 **0 基偏移**，不需要 +1。
- 区域：`holding`（HR / FC3）、`input`（IR / FC4）、`coil`（CO / FC1）、`discrete`（DI / FC2）。
- 四个区都按完整 16 位地址空间 **0–65535** 分配。即使没有定义标签，客户端也能读任意范围，
  不会因为"地址未配置"收到 illegal-address 异常；未定义地址的值默认为 0。
- 页面「数据区」直接读协议数据面（不是读标签表），默认 `03 Holding Registers`、DEC、起始 0、长度 50；
  多列网格每页 50 个，只有超过 50 才分页。起始地址可填 0 基偏移，也可填引用地址：`400100` 会转成
  偏移 99，因此显示 `400100–400149`。
- 32/64 位值占多个寄存器，**字序由 client 自己解释**：服务端每个格子就是一个 16 位寄存器，
  不做类型也不做字节序假设（`AB-CD` / `CD-AB` 是 client 侧的事）。
- 越界读写会按协议返回异常码。

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient("127.0.0.1", port=15020)   # 监听 0.0.0.0 时本机可用
client.connect()
print(client.read_holding_registers(0, count=2, device_id=1).registers)
print(client.read_holding_registers(100, count=50, device_id=1).registers)  # HR100..HR149
client.write_register(4, 1234, device_id=1)
client.close()
```

### Siemens S7

- 使用 `python-snap7 >= 3.3` 的**纯 Python** server，不依赖 `snap7.dll`，Windows / Linux 均可。
- 面积映射：`db`（需 DB 号）、`m`（Merker）、`i`（PE）、`q`（PA）。
- 地址是**字节偏移**；bool 需要指定位号，例如 `DB1.DBX0.0`、`M0.0`。
- 固定大端；默认 rack 0 / slot 1（客户端连接时传）。
- 标签地址重叠会在保存时被后端拒绝。

```python
import snap7

client = snap7.client.Client()
client.connect("10.102.128.181", 0, 0, 15102)   # rack, slot, port
print(client.db_read(1, 0, 4))                   # DB1.DBD0 (temperature)
client.db_write(1, 8, bytearray((0x02, 0x2B)))   # DB1.DBW8 = 555 (counter)
```

### OPC UA

- `asyncua` server，按标签表生成地址空间：`Objects/<实例名>/<变量名>`。
- 变量名就是 NodeId（字符串），命名空间 `urn:poly:plc`。
- 监听端口与 `endpoint_url` 是两件事：客户端拿到的 URL 必须从客户端机器可达。
  留空时自动用本机局域网 IP；非标端口或端口映射时请显式填写。
- 当前不启用加密 / 证书（后续扩展）。

```python
import asyncio
from asyncua import Client, ua

async def main():
    async with Client("opc.tcp://10.102.128.181:15400") as client:
        index = await client.get_namespace_index("urn:poly:plc")
        node = client.get_node(ua.NodeId("temperature", index))
        print(await node.read_value())
        await node.write_value(ua.Variant(888, ua.VariantType.UInt16))

asyncio.run(main())
```

## 写值与仿真

- 页面写值走 `POST /api/servers/{id}/write`，最终写进协议数据面；客户端马上能读到。
- 客户端写入同样会反映到页面（1 秒轮询）。
- 变量默认由仿真驱动（sine / random / ramp / toggle）。**非 static 的变量，外部写入会在
  下一个仿真周期被覆盖**——页面会显示 `sim` 徽标，写入后也会提示。
- 要让写入保持住，把该变量的仿真模式改成 `static`，保存标签表。
- 仿真模式：`static`、`sine`（min/max/period）、`random`（min/max）、`ramp`（min/max/step）、
  `toggle`（period）。

## 常见问题

**OpenModScan / Modbus client 连不上**
按顺序检查：

1. 实例页「监听」如果是具体 IP（例如 `10.102.128.181`），客户端必须连这个 IP；
   本机连 `127.0.0.1` 会超时。点页面上的「改为 0.0.0.0」后两者都能连。
2. 端口用实例页显示的端口（本例 15020），unit id 用 1。
3. 读 FC3（Holding Registers）；起始地址是 0 基偏移，OpenModScan 里填 100 就是 HR100。
4. 页面「数据区」能正常显示数值，说明服务端在跑；若页面也报错，先看「日志」。
5. 跨机器访问再检查 Windows 防火墙是否放行该端口。

**端口 bind 失败 / `WinError 10013`**
Windows 会保留一些端口段。换一个端口（建议从 15020 / 15102 / 15400 起），或
`netsh interface ipv4 show excludedportrange protocol=tcp` 查看保留段。同一台机器上端口全局唯一。

**页面 404 或还是旧界面**
方式 A 需要先 `npm run build`，且至少要在有 `apps/web/dist` 的情况下启动过一次 control-plane。
之后重新构建不必重启，但浏览器缓存的是旧 `index.html`，按 Ctrl+F5 强制刷新。
方式 B 确认 `/api` 代理指向 `127.0.0.1:8000`。

**Modbus 实例页没看到「实时变量」「标签表」**
这是对的：Modbus 实例只有「数据区」和「日志」。它不带标签表，也不做仿真；
要看带名字的变量请用 S7 / OPC UA 实例。

**OPC UA 客户端连不上**
先确认 `endpoint_url` 里的 IP/端口从客户端可达，再确认没有防火墙拦截。容器/端口映射场景下
宿主机端口和 endpoint 必须一致。

**改了标签表，客户端掉线**
保存会重建实例，这是预期行为。重连即可。

**control-plane 被强杀后端口仍被占用**
runtime 设计上独立存活。重新启动 control-plane 时 Supervisor 会读取 `data/runtimes.json`
清掉遗留进程；如果手工清理，结束对应的 `poly_sim_runtime` 进程即可。

**想清空所有数据**
停掉 control-plane，删除 `data/`（SQLite、实例配置、日志、PID 文件）。

## 开发

```powershell
uv sync --group dev
uv run pytest                 # 全部测试
uv run pytest -k modbus       # 单个/一组
cd apps/web
npm run typecheck
npm run build
```

Python 侧是 `uv` workspace：

```
apps/control-plane/       FastAPI：CRUD、页面 API、内嵌 Supervisor、SPA 托管
apps/web/                 React 19 + Vite + TS + zustand
apps/agent/               Supervisor：进程监督、日志、健康检查、遗留 PID 清理
apps/sim-runtime/         单实例 runtime：读 config，起一个协议 server + 仿真 tick
packages/plc-core/        标签表、编解码、仿真、协议无关逻辑
packages/contracts/       平台 ⇄ runtime 的 Pydantic 契约
packages/proto-modbus/    手写 Modbus TCP server
packages/proto-s7/        python-snap7 server 封装
packages/proto-opcua/     asyncua 地址空间
data/                     运行时生成：app.db、configs/、logs/、runtimes.json
```

### 技术选型说明

- **UI**：没有引入 mdui Web Components。Web Components 在 React 里做受控表单需要额外桥接，
  这里直接用 MD3 的 token（颜色/圆角/层级）手写原生组件，体积和可控性更好。
- **动效**：没有引入 react-bits 依赖。它是 copy-paste 组件集合，部分组件会带 three.js / gsap；
  本项目在 `components/Aurora.tsx` 与 `SpotlightCard.tsx` 里用纯 CSS transform + rAF 实现了
  同类效果，并为 `prefers-reduced-motion` 做了降级。
- **状态**：zustand 单 store；轮询在 `document.hidden` 时暂停，避免后台标签页空转。
- **协议**：Modbus 不使用 `pymodbus` 的 datastore（3.x 破坏性变更多），直接实现 TCP 帧；
  S7 用 python-snap7 的纯 Python server；OPC UA 用 asyncua 动态生成地址空间。
- **数据面即真源**：标签只是协议数据的命名视图，不做第二份内存镜像，因此客户端写入不会出现
  “平台和客户端各有一份值”的不一致。

## 限制 / 路线图

- 本机版：没有远程 agent、没有登录鉴权、没有多用户；不要直接暴露到公网。
- 只实现 SQLite（未接 Postgres/Alembic）。
- 没有 docker driver（不会用第三方模拟器镜像，docker 只用于部署我们自己的 runtime）。
- OPC UA 未启用安全策略 / 证书。
- 前端用轮询而非 WebSocket；实例数量很大时可再优化。
- 每实例一个 Python 进程，常驻约 60–100 MB，几十个实例时需要估算内存。
