# 客户端连接详情与剔除设计（Modbus）

日期：2026-11-16
状态：设计待用户复查

## 1. 问题与目标

实例页「客户端连接」目前只显示一个数字（runtime `/state` 的 `clients` 计数）。目标：**Modbus 自建实例**可以查看每条客户端连接的详情，并单独剔除（断开）某条连接。

## 2. 范围

**做**：
- Modbus sim 实例的连接列表（远端地址、连接时间、最近活动、unit_id、请求数）；
- 单独剔除某条连接；
- 剔除动作落审计。

**不做**：
- S7 / OPC UA 的连接详情与剔除（snap7 只暴露 `client_count`；asyncua 能列会话但拿不到远端 IP，留待后续）；
- 连接历史持久化（运行态，不落库）；
- 阻断被剔除客户端重连（客户端可能自动重连，平台不拉黑）。

## 3. 后端设计

### 3.1 Modbus adapter 连接跟踪

`packages/proto-modbus/poly_modbus/server.py` 的 `ModbusTcpAdapter`：

- 新增记录结构 `_ClientConn`：`id`、`conn`（socket）、`remote`（`ip:port`）、`connected_at`、`last_activity`、`unit_id`、`requests`。
- `self._conn_records: dict[str, _ClientConn]`，与 `self.clients` 计数共用 `self._clients_lock`。
- `_accept_loop`：`accept()` 后用 `conn.getpeername()` 取远端地址，生成 `id`（`uuid4().hex[:8]`），写入记录，`clients += 1`。
- `_serve`：每收到一帧更新 `last_activity`、`requests += 1`、`unit_id`（MBAP 的 unit）；`finally` 中移除记录、`clients = max(0, clients - 1)`、关闭 socket。
- `connections() -> list[dict]`：返回 `[{id, remote, connected_at, last_activity, unit_id, requests}]`，不含 socket 对象。
- `kick(conn_id)`：按 id 找到记录并 `conn.close()`；找不到抛 `KeyError`。关闭由服务端发起，`_serve` 的下一次 `recv` 会报错并退出。

### 3.2 协议适配层

`packages/plc-core/poly_core/adapter.py` 的 `ProtocolAdapter` 增加默认实现：
- `connections()` 返回 `[]`；
- `kick(conn_id)` 抛 `NotImplementedError`。

Modbus 覆盖这两个方法；S7 / OPC UA 保持默认（控制面据此返回「不支持」）。

### 3.3 runtime 控制口

`apps/sim-runtime/poly_sim_runtime/control.py` 增加：
- `GET /connections` → `{"connections": runner.adapter.connections()}`；
- `POST /connections/{conn_id}/kick` → 调 `runner.adapter.kick(conn_id)`，未知 id 返回 404，成功 `{"ok": true}`。

### 3.4 Supervisor / RemoteSupervisor

- `poly_agent.Supervisor` 增加 `connections(server_id)` 与 `kick_connection(server_id, conn_id)`，走控制口。
- `poly_control_plane.deploy.RemoteSupervisor` 同签名（经 SSH channel），保持两侧方法对齐。

### 3.5 控制面 API

- `GET /api/servers/{id}/connections`：
  - 仅 `kind=sim` 且 `protocol=modbus_tcp`；其他返回 400「仅 Modbus 实例支持连接详情」。
  - 停用实例返回 409。
- `POST /api/servers/{id}/connections/{conn_id}/kick`：
  - 同上限制；成功返回 `{"ok": true, "kicked": conn_id}`。
  - 落审计 `server.client_kick`，params `{"remote", "conn_id"}`。
- 权限：登录用户可操作自己的实例；审计记录 actor。

## 4. 前端设计

- `apps/web/src/pages/ServerPage.tsx`：Modbus sim 的「客户端连接」统计数字可点击，打开 `ClientConnectionsModal`；S7 / OPC UA 保持纯数字（不可点）。
- 新组件 `apps/web/src/components/ClientConnectionsModal.tsx`：
  - 表格列：远端地址 / 连接时间 / 最近活动 / unit_id / 请求数 / 操作；
  - 每行「断开」按钮，走 `useConfirm({ danger: true })` 二次确认；
  - 打开期间每 1 秒轮询 `GET /api/servers/{id}/connections`；
  - 断开后 toast 提示，并刷新列表；
  - 空状态「当前没有客户端连接」。
- `apps/web/src/api.ts` 增加 `serverConnections(id)` 与 `kickServerConnection(id, connId)`。

## 5. 错误处理

- 未知 `conn_id` → 404（runtime 控制口）/ 400（控制面包装为「连接已断开」）。
- 非 Modbus / 非 sim → 400；停用 → 409。
- 被剔除的客户端可能立即重连，列表会重新出现；平台不阻断，符合「模拟器不替客户端做决定」。

## 6. 测试

- **adapter 单测**：建立连接后 `connections()` 有一条记录且字段正确；`kick()` 后 `clients` 减 1、socket 关闭、记录消失；并发连接互不影响。
- **runtime 控制口**：`/connections` 与 kick 的状态码。
- **控制面 API**：Modbus sim 可查可踢；S7/OPC UA/link 返回 400；停用 409；审计有 `server.client_kick`。
- **前端**：typecheck、build；手动走查弹窗与断开。
- 现有 92 个测试保持绿。

## 7. 开放问题

- OPC UA 后续可加会话列表 + `close_session`，但没有远端 IP；是否需要留待下一期。
