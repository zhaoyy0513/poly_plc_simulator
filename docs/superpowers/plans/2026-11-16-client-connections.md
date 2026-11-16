# Modbus 客户端连接详情与剔除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Modbus 自建实例在实例页查看每条客户端连接的详情，并能单独剔除。

**Architecture:** `ModbusTcpAdapter` 跟踪每条 TCP 连接（远端地址、连接时间、最近活动、unit_id、请求数），runtime 控制口暴露 `/connections` 与 kick；`Supervisor` / `RemoteSupervisor` 同签名代理；控制面加两个 API 并落审计；前端点「客户端连接」数字弹窗展示并断开。S7 / OPC UA 走基类默认（空列表 / 未实现）。

**Tech Stack:** Python 3.10+、uv workspace、FastAPI、Pydantic v2、socket/threading、pytest、React 19、TypeScript、Vite、zustand。

---

## 文件结构

- `packages/plc-core/poly_core/adapter.py`：`ProtocolAdapter` 增加 `connections()` / `kick()` 默认实现。
- `packages/proto-modbus/poly_modbus/server.py`：`ModbusTcpAdapter` 连接跟踪、`connections()`、`kick()`。
- `apps/sim-runtime/poly_sim_runtime/control.py`：runtime 控制口 `/connections`、`/connections/{id}/kick`。
- `apps/agent/poly_agent/supervisor.py`：`Supervisor.connections()` / `kick_connection()`。
- `apps/control-plane/poly_control_plane/deploy.py`：`RemoteSupervisor.connections()` / `kick_connection()`。
- `apps/control-plane/poly_control_plane/app.py`：`GET /api/servers/{id}/connections`、`POST /api/servers/{id}/connections/{conn_id}/kick`。
- `apps/web/src/types.ts`：`ClientConnection` 类型。
- `apps/web/src/api.ts`：`serverConnections()`、`kickServerConnection()`。
- `apps/web/src/components/ClientConnectionsModal.tsx`（新建）：连接列表 + 断开。
- `apps/web/src/pages/ServerPage.tsx`：Modbus sim 的「客户端连接」可点，打开弹窗。
- `apps/web/src/styles.css`：`.stat-link`、`.conn-table` 等。
- `tests/test_client_connections.py`（新建）：adapter / runtime / 控制面 API 回归。

当前工作树已有未提交的登录 WIP 与本计划无关的改动；每个任务只提交本任务相关文件，提交信息以 `Co-Authored-By: Claude Code <noreply@anthropic.com>` 结尾。

---

### Task 1: 适配层连接跟踪（基类默认 + Modbus 实现）

**Files:**
- Modify: `packages/plc-core/poly_core/adapter.py`
- Modify: `packages/proto-modbus/poly_modbus/server.py`
- Create: `tests/test_client_connections.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_client_connections.py`：

```python
"""Modbus client-connection tracking and kick."""

from __future__ import annotations

import time

import pytest
from pymodbus.client import ModbusTcpClient

from poly_contracts import Protocol
from poly_core import ProtocolAdapter
from poly_modbus import ModbusTcpAdapter

from .util import make_config


def test_adapter_base_has_empty_connections_by_default() -> None:
    class Dummy(ProtocolAdapter):
        protocol = "dummy"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    adapter = Dummy(make_config(Protocol.MODBUS_TCP, []))
    assert adapter.connections() == []
    with pytest.raises(NotImplementedError):
        adapter.kick("nope")


def test_modbus_connections_are_tracked_and_can_be_kicked() -> None:
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        assert adapter.connections() == []

        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()
        time.sleep(0.2)

        connections = adapter.connections()
        assert len(connections) == 1
        conn = connections[0]
        assert conn["remote"].startswith("127.0.0.1:")
        assert conn["requests"] >= 1
        assert conn["unit_id"] == 1
        conn_id = conn["id"]

        adapter.kick(conn_id)
        time.sleep(0.3)
        assert adapter.connections() == []
        assert adapter.clients == 0

        client.close()
    finally:
        adapter.stop()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_client_connections.py -v`
Expected: FAIL，`ProtocolAdapter` 没有 `connections` / `kick`，`ModbusTcpAdapter` 也没有。

- [ ] **Step 3: 基类默认实现**

在 `packages/plc-core/poly_core/adapter.py` 的 `ProtocolAdapter` 中，`endpoint()` 之前加入：

```python
    def connections(self) -> list[dict[str, Any]]:
        """Live client connections. Protocols without a per-client model return []."""
        return []

    def kick(self, connection_id: str) -> None:
        """Drop one client connection. Not every protocol can do this."""
        raise NotImplementedError(f"{self.protocol} does not support kicking clients")
```

- [ ] **Step 4: Modbus 连接跟踪**

在 `packages/proto-modbus/poly_modbus/server.py`：

顶部 import 增加 `time`、`uuid`、`dataclass`：

```python
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
```

在 `MBAP = struct.Struct(">HHHB")` 之后加入：

```python
@dataclass
class _ClientConn:
    id: str
    conn: socket.socket
    remote: str
    connected_at: float
    last_activity: float
    unit_id: int | None = None
    requests: int = 0
```

`ModbusTcpAdapter.__init__` 中，`self._clients_lock = threading.Lock()` 之后加入：

```python
        self._conn_records: dict[str, _ClientConn] = {}
```

`_accept_loop` 中，`client, _ = self._sock.accept()` 改为：

```python
                client, address = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._clients_lock:
                self.clients += 1
                conn_id = uuid.uuid4().hex[:8]
                now = time.time()
                self._conn_records[conn_id] = _ClientConn(
                    id=conn_id,
                    conn=client,
                    remote=f"{address[0]}:{address[1]}",
                    connected_at=now,
                    last_activity=now,
                )
            thread = threading.Thread(
                target=self._serve, args=(client, conn_id), daemon=True
            )
            thread.start()
            self._threads.append(thread)
```

`_serve` 签名改为 `def _serve(self, conn: socket.socket, conn_id: str) -> None:`，并在解包 MBAP 后更新记录：

```python
                tid, pid, length, unit = MBAP.unpack(header)
                with self._clients_lock:
                    record = self._conn_records.get(conn_id)
                    if record is not None:
                        record.last_activity = time.time()
                        record.unit_id = unit
                        record.requests += 1
```

`finally` 块改为：

```python
        finally:
            try:
                conn.close()
            finally:
                with self._clients_lock:
                    self._conn_records.pop(conn_id, None)
                    self.clients = max(0, self.clients - 1)
```

在 `endpoint()` 之前加入：

```python
    def connections(self) -> list[dict[str, Any]]:
        with self._clients_lock:
            return [
                {
                    "id": record.id,
                    "remote": record.remote,
                    "connected_at": record.connected_at,
                    "last_activity": record.last_activity,
                    "unit_id": record.unit_id,
                    "requests": record.requests,
                }
                for record in self._conn_records.values()
            ]

    def kick(self, connection_id: str) -> None:
        with self._clients_lock:
            record = self._conn_records.get(connection_id)
            if record is None:
                raise KeyError(f"unknown connection: {connection_id}")
            try:
                record.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            record.conn.close()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_client_connections.py -v`
Expected: 2 passed。

- [ ] **Step 6: 提交**

```bash
git add packages/plc-core/poly_core/adapter.py packages/proto-modbus/poly_modbus/server.py tests/test_client_connections.py
git commit -m "feat: track Modbus client connections" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: runtime 控制口 + Supervisor 代理

**Files:**
- Modify: `apps/sim-runtime/poly_sim_runtime/control.py`
- Modify: `apps/agent/poly_agent/supervisor.py`
- Modify: `apps/control-plane/poly_control_plane/deploy.py`
- Modify: `tests/test_client_connections.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_client_connections.py` 追加：

```python
def test_runtime_control_lists_and_kicks_connections() -> None:
    from fastapi.testclient import TestClient

    from poly_sim_runtime.control import create_app
    from poly_sim_runtime.runner import RuntimeRunner

    config = make_config(Protocol.MODBUS_TCP, [])
    runner = RuntimeRunner(config)
    runner.start()
    time.sleep(0.2)
    try:
        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()
        time.sleep(0.2)
        with TestClient(create_app(runner)) as http:
            data = http.get("/connections").json()
            assert len(data["connections"]) == 1
            conn_id = data["connections"][0]["id"]

            response = http.post(f"/connections/{conn_id}/kick")
            assert response.status_code == 200
            assert response.json()["kicked"] == conn_id
            time.sleep(0.3)
            assert http.get("/connections").json()["connections"] == []

            assert http.post("/connections/nope/kick").status_code == 404
        client.close()
    finally:
        runner.stop()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_client_connections.py::test_runtime_control_lists_and_kicks_connections -v`
Expected: FAIL，`/connections` 返回 404。

- [ ] **Step 3: runtime 控制口**

在 `apps/sim-runtime/poly_sim_runtime/control.py` 的 `write_area` 之后加入：

```python
    @app.get("/connections")
    def connections() -> dict[str, Any]:
        adapter = runner.adapter
        return {"connections": adapter.connections() if adapter is not None else []}

    @app.post("/connections/{conn_id}/kick")
    def kick_connection(conn_id: str) -> dict[str, Any]:
        adapter = runner.adapter
        if adapter is None:
            raise HTTPException(status_code=404, detail="实例未在运行")
        try:
            adapter.kick(conn_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotImplementedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "kicked": conn_id}
```

- [ ] **Step 4: Supervisor 代理**

在 `apps/agent/poly_agent/supervisor.py` 的 `write_area` 之后加入：

```python
    def connections(self, server_id: str) -> list[dict[str, Any]]:
        managed = self._servers.get(server_id)
        if managed is None or managed.status != "running":
            raise RuntimeError("实例未在运行")
        response = httpx.get(
            f"http://127.0.0.1:{managed.control_port}/connections",
            timeout=3.0,
        )
        if response.status_code >= 400:
            raise ValueError(response.json().get("detail", response.text))
        return list(response.json().get("connections", []))

    def kick_connection(self, server_id: str, conn_id: str) -> None:
        managed = self._servers.get(server_id)
        if managed is None or managed.status != "running":
            raise RuntimeError("实例未在运行")
        response = httpx.post(
            f"http://127.0.0.1:{managed.control_port}/connections/{conn_id}/kick",
            timeout=3.0,
        )
        if response.status_code >= 400:
            raise ValueError(response.json().get("detail", response.text))
```

- [ ] **Step 5: RemoteSupervisor 代理**

在 `apps/control-plane/poly_control_plane/deploy.py` 的 `write_area` 之后加入：

```python
    def connections(self, server_id: str) -> list[dict[str, Any]]:
        data = self._get(server_id, "/connections")
        return list(data.get("connections", []))

    def kick_connection(self, server_id: str, conn_id: str) -> None:
        self._post(server_id, f"/connections/{conn_id}/kick", {})
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_client_connections.py -v`
Expected: 3 passed。

- [ ] **Step 7: 提交**

```bash
git add apps/sim-runtime/poly_sim_runtime/control.py apps/agent/poly_agent/supervisor.py apps/control-plane/poly_control_plane/deploy.py tests/test_client_connections.py
git commit -m "feat: expose client connections through runtime and supervisors" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 控制面 API + 审计

**Files:**
- Modify: `apps/control-plane/poly_control_plane/app.py`
- Modify: `tests/test_client_connections.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_client_connections.py` 追加（复用 `tests/test_creation_defaults_api.py` 的 `api` fixture 写法，在本文件内定义同构 fixture）：

```python
import os
import subprocess
import sys

import httpx
import pytest

from poly_control_plane.system import ROOT
from tests.conftest import free_port


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    data = tmp_path_factory.mktemp("poly-connections-api")
    port = free_port()
    env = {**os.environ, "POLY_DATA_DIR": str(data)}
    log = (data / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "poly_control_plane", "--init-admin", "admin1",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(300):
            if process.poll() is not None:
                raise RuntimeError("control plane exited")
            try:
                if httpx.get(f"{base}/api/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("control plane did not start")
        with httpx.Client(base_url=base, timeout=15, trust_env=False) as http:
            assert http.post(
                "/api/login", json={"username": "admin1", "password": "123456"}
            ).status_code == 204
            yield http
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()


def _modbus_sim(api: httpx.Client) -> str:
    site = api.post("/api/sites", json={"name": "conn-site", "host": ""}).json()
    response = api.post(
        f"/api/sites/{site['id']}/servers",
        json={"name": "mb", "protocol": "modbus_tcp", "port": free_port()},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_connections_endpoint_is_modbus_only(api: httpx.Client) -> None:
    server_id = _modbus_sim(api)
    response = api.get(f"/api/servers/{server_id}/connections")
    assert response.status_code == 200
    assert response.json() == {"connections": []}

    site = api.post("/api/sites", json={"name": "s7-site", "host": ""}).json()
    s7 = api.post(
        f"/api/sites/{site['id']}/servers",
        json={"name": "s7", "protocol": "s7", "port": free_port()},
    )
    assert s7.status_code == 201, s7.text
    assert api.get(f"/api/servers/{s7.json()['id']}/connections").status_code == 400


def test_kick_unknown_connection_reports_gone(api: httpx.Client) -> None:
    server_id = _modbus_sim(api)
    response = api.post(f"/api/servers/{server_id}/connections/nope/kick")
    assert response.status_code == 400
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_client_connections.py -k "connections_endpoint or kick_unknown" -v`
Expected: FAIL，`/connections` 返回 404。

- [ ] **Step 3: 控制面 API**

在 `apps/control-plane/poly_control_plane/app.py` 的 `server_values` 之后加入：

```python
@app.get("/api/servers/{server_id}/connections")
def server_connections(
    server_id: str, request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = _server(session, server_id)
    if row.is_link or row.protocol != "modbus_tcp":
        raise HTTPException(status_code=400, detail="仅 Modbus 实例支持连接详情")
    if not row.enabled:
        raise HTTPException(status_code=409, detail="实例已停用")
    supervisor, _ = _runtime(request.app, session, row.site_id)
    return {"connections": supervisor.connections(server_id)}


@app.post("/api/servers/{server_id}/connections/{conn_id}/kick")
def kick_server_connection(
    server_id: str,
    conn_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = _server(session, server_id)
    if row.is_link or row.protocol != "modbus_tcp":
        raise HTTPException(status_code=400, detail="仅 Modbus 实例支持连接详情")
    if not row.enabled:
        raise HTTPException(status_code=409, detail="实例已停用")
    supervisor, _ = _runtime(request.app, session, row.site_id)
    remote = next(
        (item.get("remote") for item in supervisor.connections(server_id) if item.get("id") == conn_id),
        None,
    )
    try:
        supervisor.kick_connection(server_id, conn_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        session,
        "server.client_kick",
        f"{server_id}/{conn_id}",
        remote or "",
        params={"remote": remote, "conn_id": conn_id},
    )
    session.commit()
    return {"ok": True, "kicked": conn_id}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_client_connections.py -v`
Expected: 5 passed。

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/poly_control_plane/app.py tests/test_client_connections.py
git commit -m "feat: add connection list and kick API" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: 前端连接弹窗

**Files:**
- Modify: `apps/web/src/types.ts`
- Modify: `apps/web/src/api.ts`
- Create: `apps/web/src/components/ClientConnectionsModal.tsx`
- Modify: `apps/web/src/pages/ServerPage.tsx`
- Modify: `apps/web/src/styles.css`

- [ ] **Step 1: 类型与 API**

在 `apps/web/src/types.ts` 的 `RuntimeState` 之后加入：

```ts
export interface ClientConnection {
  id: string
  remote: string
  connected_at: number
  last_activity: number
  unit_id: number | null
  requests: number
}
```

在 `apps/web/src/api.ts` 的 `values` 附近加入：

```ts
  serverConnections: (id: string) =>
    request<{ connections: ClientConnection[] }>(`/api/servers/${id}/connections`),
  kickServerConnection: (id: string, connId: string) =>
    request<void>(`/api/servers/${id}/connections/${connId}/kick`, { method: 'POST' }),
```

并在 `api.ts` 顶部的 type import 中加入 `ClientConnection`。

- [ ] **Step 2: 连接弹窗组件**

创建 `apps/web/src/components/ClientConnectionsModal.tsx`：

```tsx
import { useState } from 'react'
import { api } from '../api'
import { useConfirm } from './ConfirmModal'
import { Icon } from './Icon'
import { Modal } from './Modal'
import { usePolling } from '../hooks/usePolling'
import type { ClientConnection } from '../types'

interface Props {
  open: boolean
  serverId: string
  onClose: () => void
}

function formatTime(value: number): string {
  return new Date(value * 1000).toLocaleTimeString()
}

export function ClientConnectionsModal({ open, serverId, onClose }: Props) {
  const confirm = useConfirm()
  const [connections, setConnections] = useState<ClientConnection[]>([])
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState<string | null>(null)

  usePolling(
    () => {
      if (!open) return
      void (async () => {
        try {
          const data = await api.serverConnections(serverId)
          setConnections(data.connections)
          setError('')
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err))
        }
      })()
    },
    1000,
    open,
  )

  const kick = async (conn: ClientConnection) => {
    const ok = await confirm({
      title: '断开客户端',
      message: `确定断开 ${conn.remote}？客户端可能会自动重连。`,
      confirmText: '断开',
      danger: true,
    })
    if (!ok) return
    setBusyId(conn.id)
    try {
      await api.kickServerConnection(serverId, conn.id)
      setConnections((list) => list.filter((item) => item.id !== conn.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <Modal open={open} title="客户端连接" subtitle="Modbus 自建实例的实时连接" width={640} onClose={onClose}>
      {error ? <p className="login-error">{error}</p> : null}
      <div className="conn-table">
        <div className="conn-row conn-head">
          <span>远端地址</span>
          <span>连接时间</span>
          <span>最近活动</span>
          <span>unit</span>
          <span>请求数</span>
          <span />
        </div>
        {connections.length === 0 ? (
          <p className="muted">当前没有客户端连接</p>
        ) : (
          connections.map((conn) => (
            <div key={conn.id} className="conn-row">
              <span className="mono">{conn.remote}</span>
              <span>{formatTime(conn.connected_at)}</span>
              <span>{formatTime(conn.last_activity)}</span>
              <span>{conn.unit_id ?? '—'}</span>
              <span>{conn.requests}</span>
              <button
                type="button"
                className="btn tiny danger"
                disabled={busyId === conn.id}
                onClick={() => void kick(conn)}
              >
                <Icon name="x" size={13} /> 断开
              </button>
            </div>
          ))
        )}
      </div>
    </Modal>
  )
}
```

- [ ] **Step 3: 接入实例页**

在 `apps/web/src/pages/ServerPage.tsx`：

顶部 import 增加：

```tsx
import { ClientConnectionsModal } from '../components/ClientConnectionsModal'
```

在 `const [mapBusy, setMapBusy] = useState(false)` 之后加入：

```tsx
  const [connectionsOpen, setConnectionsOpen] = useState(false)
```

把「客户端连接」统计块改为可点（仅 Modbus 自建）：

```tsx
        <div className="stat">
          <small>{link ? '往返延迟' : '客户端连接'}</small>
          {link || !isModbus ? (
            <strong>
              {link
                ? latency != null
                  ? `${latency} ms`
                  : '—'
                : (values?.clients ?? current.clients)}
            </strong>
          ) : (
            <button
              type="button"
              className="stat-link"
              onClick={() => setConnectionsOpen(true)}
              title="查看客户端连接"
            >
              {values?.clients ?? current.clients}
            </button>
          )}
          {link ? (
            <span className="stat-note">{latency != null ? '最近一次探测' : '未连接'}</span>
          ) : null}
        </div>
```

在 `ServerPage` 返回的 JSX 末尾（与 `</div>` 同级）渲染：

```tsx
      <ClientConnectionsModal
        open={connectionsOpen}
        serverId={serverId}
        onClose={() => setConnectionsOpen(false)}
      />
```

- [ ] **Step 4: 样式**

在 `apps/web/src/styles.css` 的 `.stat` 相关规则附近加入：

```css
.stat-link {
  background: none;
  border: 0;
  padding: 0;
  color: var(--text);
  font-size: 18px;
  font-weight: 600;
  cursor: pointer;
}

.stat-link:hover {
  color: var(--primary);
  text-decoration: underline;
}

.conn-table {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.conn-row {
  display: grid;
  grid-template-columns: 1.4fr 1fr 1fr 0.6fr 0.7fr auto;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  font-size: 13px;
}

.conn-head {
  border: 0;
  color: var(--muted);
  font-size: 12px;
}
```

- [ ] **Step 5: 跑验证**

Run: `npm --prefix apps/web run typecheck`
Expected: exit 0。
Run: `npm --prefix apps/web run build`
Expected: built successfully。

- [ ] **Step 6: 提交**

```bash
git add apps/web/src/types.ts apps/web/src/api.ts apps/web/src/components/ClientConnectionsModal.tsx apps/web/src/pages/ServerPage.tsx apps/web/src/styles.css
git commit -m "feat: show Modbus client connections and allow kick" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: 文档与全量验证

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`

- [ ] **Step 1: 文档**

`AGENTS.md` 的「协议层注意点」或「约定」补一条：Modbus 实例页「客户端连接」可点开查看每条连接（远端地址/连接时间/最近活动/unit/请求数）并单独断开；仅 Modbus 自建实例支持，S7/OPC UA 只显示数量。剔除落审计 `server.client_kick`。

`README.md` 的实例页说明补一句：Modbus 实例的「客户端连接」数字可点开查看与断开。

- [ ] **Step 2: 全量验证**

Run: `uv run pytest`
Expected: 全绿（原 92 + 新增 5 = 97）。
Run: `npm --prefix apps/web run typecheck`
Expected: exit 0。
Run: `npm --prefix apps/web run check:address`
Expected: `全部通过（31 条）`。
Run: `npm --prefix apps/web run build`
Expected: built successfully。
Run: `git diff --check`
Expected: 无输出。

- [ ] **Step 3: 手动走查**

启动控制面与前端，建一个 Modbus 实例，用真实 Modbus client 连上；实例页点「客户端连接」数字，应看到该连接；点「断开」→ 二次确认 → 列表清空、client 侧连接中断。

- [ ] **Step 4: 提交**

```bash
git add AGENTS.md README.md
git commit -m "docs: describe Modbus client connection panel" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 计划自检

- **Spec coverage**：连接跟踪（Task 1）、runtime/Supervisor（Task 2）、控制面 API + 审计（Task 3）、前端弹窗（Task 4）、文档与验证（Task 5）。非 Modbus 返回 400、停用 409、未知 conn 400 均有测试。
- **Placeholder scan**：无 TBD/TODO；每个代码步骤都给了完整片段。
- **Type consistency**：`connections()` / `kick()` / `kick_connection()` / `conn_id` / `ClientConnection` 字段（`id/remote/connected_at/last_activity/unit_id/requests`）全程一致；runtime 返回 `{"connections": [...]}`，kick 返回 `{"ok": True, "kicked": conn_id}`。
