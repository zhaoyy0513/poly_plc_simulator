# 修复/清理（重置数据区 + 清理测试残留）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Modbus/S7/OPC UA 自建实例可「重置数据区」（保持 client 连接、只清零），并提供「仅测试残留」的确认清理；修测试夹具根因。最高约束：不影响其他运行中的业务服务。

**Architecture:** `ProtocolAdapter.reset()` 由各协议实现（清零自己的数据面，不动 socket/连接）；runtime `POST /reset` → `Supervisor.reset`/`RemoteSupervisor.reset` → 控制面 `POST /api/servers/{id}/reset`（sim only、审计）。新增 `maintenance.py` 用 psutil 枚举 `poly_sim_runtime`，`owned`（在本数据目录清单）与 `is_test`（config 在临时/pytest 目录）双判定，kill 只接受 `is_test && !owned`。

**Tech Stack:** Python 3.10+、uv、FastAPI、Pydantic v2、psutil、pytest、React 19、TS、Vite、zustand。

---

## 文件结构

- `packages/plc-core/poly_core/adapter.py`：`reset()` 默认。
- `packages/proto-modbus/poly_modbus/server.py`、`packages/proto-s7/poly_s7/server.py`、`packages/proto-opcua/poly_opcua/server.py`：`reset()`。
- `apps/sim-runtime/poly_sim_runtime/control.py`：`POST /reset`。
- `apps/agent/poly_agent/supervisor.py`：`reset()`。
- `apps/control-plane/poly_control_plane/deploy.py`：`RemoteSupervisor.reset()`。
- `apps/control-plane/poly_control_plane/maintenance.py`（新）：`list_runtimes` / `kill_test_runtimes`。
- `apps/control-plane/poly_control_plane/app.py`：reset + maintenance 路由。
- `apps/web/src/api.ts`、`apps/web/src/pages/ServerPage.tsx`、`apps/web/src/components/MaintenanceModal.tsx`（新）、`apps/web/src/styles.css`。
- `tests/util.py`：`kill_runtimes_under`；`tests/test_client_connections.py`、`tests/test_creation_defaults_api.py`：fixture teardown 清理。
- `tests/test_maintenance.py`（新）：reset + maintenance 回归。

---

### Task 1: 协议适配层 reset

**Files:**
- Modify: `packages/plc-core/poly_core/adapter.py`
- Modify: `packages/proto-modbus/poly_modbus/server.py`
- Modify: `packages/proto-s7/poly_s7/server.py`
- Modify: `packages/proto-opcua/poly_opcua/server.py`
- Create: `tests/test_maintenance.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_maintenance.py`：

```python
"""Reset clears the data plane without dropping clients."""

from __future__ import annotations

import time

from pymodbus.client import ModbusTcpClient

from poly_contracts import Protocol
from poly_modbus import ModbusTcpAdapter

from .util import make_config


def test_modbus_reset_zeroes_areas_and_keeps_clients() -> None:
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()
        assert client.write_register(5, 1234, device_id=1)
        assert client.write_coil(2, True, device_id=1)
        time.sleep(0.1)
        assert adapter.read_area("holding", 5, 1) == [1234]

        adapter.reset()

        assert adapter.read_area("holding", 5, 1) == [0]
        assert adapter.read_area("coil", 2, 1) == [False]
        # connection survived the reset
        assert adapter.clients == 1
        assert not client.read_holding_registers(5, count=1, device_id=1).isError()
        client.close()
    finally:
        adapter.stop()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_maintenance.py -v`
Expected: FAIL，`ModbusTcpAdapter` 没有 `reset`。

- [ ] **Step 3: 基类 + 三协议 reset**

`packages/plc-core/poly_core/adapter.py`，在 `kick()` 之后加：

```python
    def reset(self) -> None:
        """Zero the data plane in place, keeping client connections alive."""
        raise NotImplementedError(f"{self.protocol} does not support reset")
```

`packages/proto-modbus/poly_modbus/server.py`，在 `kick()` 之后加：

```python
    def reset(self) -> None:
        with self._lock:
            self._holding[:] = [0] * self.area_size
            self._input[:] = [0] * self.area_size
            self._coils[:] = [False] * self.area_size
            self._discrete[:] = [False] * self.area_size
```

`packages/proto-s7/poly_s7/server.py`，在 `write_tag()` 之后加：

```python
    def reset(self) -> None:
        with self._lock:
            for key in list(self._buffers):
                self._buffers[key] = bytearray(len(self._buffers[key]))
```

`packages/proto-opcua/poly_opcua/server.py`，在 `write_tag()` 之后加：

```python
    def reset(self) -> None:
        for tag in self.tags.values():
            if tag.name in self._nodes:
                self.write_tag(tag, coerce(seed_value(tag), tag.data_type))
```

（`coerce`、`seed_value` 已在该文件 import。）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_maintenance.py -v`
Expected: 1 passed。

- [ ] **Step 5: 提交**

```bash
git add packages/plc-core/poly_core/adapter.py packages/proto-modbus/poly_modbus/server.py packages/proto-s7/poly_s7/server.py packages/proto-opcua/poly_opcua/server.py tests/test_maintenance.py
git commit -m "feat: add in-place data-plane reset to adapters" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: runtime /reset + Supervisor 代理

**Files:**
- Modify: `apps/sim-runtime/poly_sim_runtime/control.py`
- Modify: `apps/agent/poly_agent/supervisor.py`
- Modify: `apps/control-plane/poly_control_plane/deploy.py`
- Modify: `tests/test_maintenance.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_maintenance.py` 追加：

```python
def test_runtime_reset_endpoint_zeroes_data() -> None:
    from fastapi.testclient import TestClient

    from poly_sim_runtime.control import create_app
    from poly_sim_runtime.runner import RuntimeRunner

    config = make_config(Protocol.MODBUS_TCP, [])
    runner = RuntimeRunner(config)
    runner.start()
    time.sleep(0.2)
    try:
        runner.write_area("holding", 3, 77)
        with TestClient(create_app(runner)) as http:
            assert http.post("/reset").status_code == 200
        assert runner.read_area("holding", 3, 1) == [0]
    finally:
        runner.stop()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_maintenance.py::test_runtime_reset_endpoint_zeroes_data -v`
Expected: FAIL，`/reset` 404。

- [ ] **Step 3: runtime /reset**

`apps/sim-runtime/poly_sim_runtime/control.py`，在 kick 端点之后加：

```python
    @app.post("/reset")
    def reset() -> dict[str, Any]:
        adapter = runner.adapter
        if adapter is None:
            raise HTTPException(status_code=404, detail="实例未在运行")
        try:
            adapter.reset()
        except NotImplementedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}
```

- [ ] **Step 4: Supervisor.reset**

`apps/agent/poly_agent/supervisor.py`，在 `kick_connection()` 之后加：

```python
    def reset(self, server_id: str) -> None:
        managed = self._servers.get(server_id)
        if managed is None or managed.status != "running":
            raise RuntimeError("实例未在运行")
        response = httpx.post(
            f"http://127.0.0.1:{managed.control_port}/reset",
            timeout=3.0,
        )
        if response.status_code >= 400:
            raise ValueError(response.json().get("detail", response.text))
```

- [ ] **Step 5: RemoteSupervisor.reset**

`apps/control-plane/poly_control_plane/deploy.py`，在 `kick_connection()` 之后加：

```python
    def reset(self, server_id: str) -> None:
        self._post(server_id, "/reset", {})
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_maintenance.py -v`
Expected: 2 passed。

- [ ] **Step 7: 提交**

```bash
git add apps/sim-runtime/poly_sim_runtime/control.py apps/agent/poly_agent/supervisor.py apps/control-plane/poly_control_plane/deploy.py tests/test_maintenance.py
git commit -m "feat: expose reset through runtime and supervisors" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 控制面 reset API + 审计

**Files:**
- Modify: `apps/control-plane/poly_control_plane/app.py`
- Modify: `tests/test_maintenance.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_maintenance.py` 追加（复用 `tests/test_client_connections.py` 的 `api` fixture 写法，本文件内定义同构 fixture；或直接 import 复用——为简单，本文件自带 fixture，参照 `test_client_connections.py` 的 `api`）：

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
    data = tmp_path_factory.mktemp("poly-maintenance-api")
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
            assert http.post("/api/login", json={"username": "admin1", "password": "123456"}).status_code == 204
            yield http
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()


def _sim(api, protocol="modbus_tcp"):
    site = api.post("/api/sites", json={"name": "m-site", "host": ""}).json()
    r = api.post(f"/api/sites/{site['id']}/servers",
                 json={"name": "m", "protocol": protocol, "port": free_port()})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_reset_zeroes_and_audits(api) -> None:
    server_id = _sim(api)
    assert api.post(f"/api/servers/{server_id}/area",
                    json={"area": "holding", "address": 1, "value": 9}).status_code == 200
    assert api.post(f"/api/servers/{server_id}/reset").status_code == 200
    area = api.get(f"/api/servers/{server_id}/area?area=holding&address=1&count=1").json()
    assert area["values"] == [0]
    audit = api.get("/api/audit").json()
    assert audit[0]["action"] == "server.reset"


def test_reset_rejects_link_and_disabled(api) -> None:
    sim_id = _sim(api)
    link = api.post(f"/api/sites/{api.get('/api/sites').json()[0]['id']}/servers",
                    json={"name": "lk", "kind": "link", "protocol": "modbus_tcp",
                          "port": 502, "target_host": "127.0.0.1", "target_port": 502})
    # link probe may fail if nothing on 502; use the sim's own port as target instead
    sim = api.get(f"/api/servers/{sim_id}").json()
    link = api.post(f"/api/sites/{sim['site_id']}/servers",
                    json={"name": "lk", "kind": "link", "protocol": "modbus_tcp",
                          "port": sim["port"], "target_host": "127.0.0.1", "target_port": sim["port"]})
    assert link.status_code == 201, link.text
    assert api.post(f"/api/servers/{link.json()['id']}/reset").status_code == 400

    assert api.patch(f"/api/servers/{sim_id}", json={"enabled": False}).status_code == 200
    assert api.post(f"/api/servers/{sim_id}/reset").status_code == 409
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_maintenance.py -k reset -v`
Expected: FAIL，`/reset` 404/405。

- [ ] **Step 3: 控制面 reset 路由**

`apps/control-plane/poly_control_plane/app.py`，在 `kick_server_connection` 之后加：

```python
@app.post("/api/servers/{server_id}/reset")
def reset_server(
    server_id: str, request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = _server(session, server_id)
    if row.is_link:
        raise HTTPException(status_code=400, detail="连接实例的数据在对方设备，不能重置")
    if not row.enabled:
        raise HTTPException(status_code=409, detail="实例已停用")
    supervisor, _ = _runtime(request.app, session, row.site_id)
    try:
        supervisor.reset(server_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(session, "server.reset", server_id, row.protocol, params={"protocol": row.protocol})
    session.commit()
    return {"ok": True}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_maintenance.py -v`
Expected: 4 passed。

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/poly_control_plane/app.py tests/test_maintenance.py
git commit -m "feat: add reset API with audit" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: maintenance 枚举 + 仅测试残留清理

**Files:**
- Create: `apps/control-plane/poly_control_plane/maintenance.py`
- Modify: `apps/control-plane/poly_control_plane/app.py`
- Modify: `tests/test_maintenance.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_maintenance.py` 追加（直接测 maintenance 函数，不依赖 HTTP）：

```python
def test_maintenance_only_kills_test_orphans(tmp_path) -> None:
    import json

    from poly_control_plane import maintenance

    # a fake "owned" manifest entry and a fake test orphan are simulated by
    # monkeypatching list_runtimes; here we test the validation logic directly.
    rows = [
        {"pid": 111, "create_time": 1.0, "config": str(tmp_path / "pytest-of-x" / "a.json"), "control_port": 1, "owned": False, "is_test": True},
        {"pid": 222, "create_time": 2.0, "config": str(tmp_path / "real" / "b.json"), "control_port": 2, "owned": False, "is_test": False},
        {"pid": 333, "create_time": 3.0, "config": str(tmp_path / "pytest-of-x" / "c.json"), "control_port": 3, "owned": True, "is_test": True},
    ]
    import poly_control_plane.maintenance as m
    orig = m.list_runtimes
    m.list_runtimes = lambda data_dir: rows
    try:
        # owned and non-test must be refused
        with pytest.raises(ValueError):
            m.kill_test_runtimes([222], tmp_path)
        with pytest.raises(ValueError):
            m.kill_test_runtimes([333], tmp_path)
        with pytest.raises(ValueError):
            m.kill_test_runtimes([111, 222], tmp_path)
    finally:
        m.list_runtimes = orig
```

（kill 的真实 psutil 路径由 Task 6 的夹具清理间接覆盖；这里锁住「owned/非 test 拒杀」的安全逻辑。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_maintenance.py::test_maintenance_only_kills_test_orphans -v`
Expected: FAIL，`maintenance` 模块不存在。

- [ ] **Step 3: maintenance.py**

创建 `apps/control-plane/poly_control_plane/maintenance.py`：

```python
"""List local poly_sim_runtime processes and kill only test leftovers.

Safety: a process that is in this data dir's manifest (owned) or whose config
is not under a temp/pytest dir is never killed — it may be another control
plane's live business instance.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import psutil


def _owned_identities(data_dir: Path) -> set[tuple[int, float]]:
    owned: set[tuple[int, float]] = set()
    try:
        raw = json.loads((data_dir / "runtimes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return owned
    for record in raw.values():
        if isinstance(record, dict):
            owned.add((int(record.get("pid") or 0), float(record.get("create_time") or 0.0)))
        elif isinstance(record, int):
            owned.add((record, 0.0))
    return owned


def _is_test_config(config: str) -> bool:
    if not config:
        return False
    if "pytest-of" in config:
        return True
    try:
        return Path(config).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):
        return False


def list_runtimes(data_dir: Path) -> list[dict[str, Any]]:
    owned = _owned_identities(data_dir)
    out: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not any("poly_sim_runtime" in part for part in cmdline):
            continue
        config = ""
        control_port = 0
        for i, part in enumerate(cmdline):
            if part == "--config" and i + 1 < len(cmdline):
                config = cmdline[i + 1]
            elif part == "--control-port" and i + 1 < len(cmdline):
                try:
                    control_port = int(cmdline[i + 1])
                except ValueError:
                    pass
        pid = proc.info["pid"]
        create_time = proc.info["create_time"] or 0.0
        out.append({
            "pid": pid,
            "create_time": create_time,
            "config": config,
            "control_port": control_port,
            "owned": (pid, create_time) in owned or (pid, 0.0) in owned,
            "is_test": _is_test_config(config),
        })
    return out


def kill_test_runtimes(pids: list[int], data_dir: Path) -> list[int]:
    rows = {r["pid"]: r for r in list_runtimes(data_dir)}
    for pid in pids:
        row = rows.get(pid)
        if row is None or row["owned"] or not row["is_test"]:
            raise ValueError(f"pid {pid} 不是可清理的测试残留")
    killed: list[int] = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
            killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed
```

- [ ] **Step 4: 控制面 maintenance 路由**

`apps/control-plane/poly_control_plane/app.py`：顶部 import `from .maintenance import kill_test_runtimes, list_runtimes`；在 reset 路由之后加：

```python
class KillBody(BaseModel):
    pids: list[int]


@app.get("/api/maintenance/runtimes")
def maintenance_runtimes(request: Request) -> dict[str, Any]:
    return {"runtimes": list_runtimes(data_dir())}


@app.post("/api/maintenance/kill")
def maintenance_kill(
    body: KillBody,
    request: Request,
    _: UserRow = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        killed = kill_test_runtimes(body.pids, data_dir())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(session, "maintenance.kill", "", ",".join(map(str, killed)), params={"pids": killed})
    session.commit()
    return {"killed": killed}
```

（`BaseModel` 若未 import 则加 `from pydantic import BaseModel`；`require_admin`、`data_dir` 已存在。）

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_maintenance.py -v`
Expected: 5 passed。

- [ ] **Step 6: 提交**

```bash
git add apps/control-plane/poly_control_plane/maintenance.py apps/control-plane/poly_control_plane/app.py tests/test_maintenance.py
git commit -m "feat: list runtimes and kill only test leftovers" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: 前端 reset 按钮 + 清理弹窗

**Files:**
- Modify: `apps/web/src/api.ts`
- Modify: `apps/web/src/pages/ServerPage.tsx`
- Create: `apps/web/src/components/MaintenanceModal.tsx`
- Modify: `apps/web/src/styles.css`

- [ ] **Step 1: API**

`apps/web/src/api.ts` 加：

```ts
  resetServer: (id: string) => request<{ ok: boolean }>(`/api/servers/${id}/reset`, { method: 'POST' }),
  maintenanceRuntimes: () => request<{ runtimes: RuntimeProcess[] }>('/api/maintenance/runtimes'),
  killRuntimes: (pids: number[]) => request<{ killed: number[] }>('/api/maintenance/kill', json('POST', { pids })),
```

`apps/web/src/types.ts` 加：

```ts
export interface RuntimeProcess {
  pid: number
  create_time: number
  config: string
  control_port: number
  owned: boolean
  is_test: boolean
}
```

并在 api.ts type import 加 `RuntimeProcess`。

- [ ] **Step 2: ServerPage reset 按钮**

在 head-actions 的「重启」按钮附近，仅 `!link` 时加：

```tsx
          {!link ? (
            <button
              type="button"
              className="btn ghost"
              disabled={!running}
              onClick={() => void resetData()}
            >
              <Icon name="restart" size={15} /> 重置数据区
            </button>
          ) : null}
```

并加 handler：

```tsx
  const resetData = async () => {
    const ok = await confirm({
      title: '重置数据区',
      message: '把该实例的数据清零（保持 client 连接）。已写入的值会丢失，确定？',
      confirmText: '重置',
      danger: true,
    })
    if (!ok || !current) return
    try {
      await api.resetServer(current.id)
      notify('ok', '数据区已重置')
      void refreshValues(serverId)
    } catch (err) {
      notify('error', err instanceof Error ? err.message : String(err))
    }
  }
```

（`api` 若未 import 则加 `import { api } from '../api'`。）

- [ ] **Step 3: MaintenanceModal**

创建 `apps/web/src/components/MaintenanceModal.tsx`：列出 runtimes，checkbox 默认勾选 `is_test && !owned`，`owned` 行禁用；「清理所选」走 `useConfirm`，调 `killRuntimes`，刷新。入口：在 `App.tsx` 侧边栏底部（管理员）加一个「清理残留」按钮打开它（或放在系统信息区）。表格复用 `.conn-table`/`.conn-row` 样式。

- [ ] **Step 4: 验证**

Run: `npm --prefix apps/web run typecheck` → exit 0。
Run: `npm --prefix apps/web run build` → built。

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/api.ts apps/web/src/types.ts apps/web/src/pages/ServerPage.tsx apps/web/src/components/MaintenanceModal.tsx apps/web/src/App.tsx apps/web/src/styles.css
git commit -m "feat: reset button and test-orphan cleanup UI" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: 修测试夹具 + 文档 + 全量验证

**Files:**
- Modify: `tests/util.py`
- Modify: `tests/test_client_connections.py`
- Modify: `tests/test_creation_defaults_api.py`
- Modify: `AGENTS.md`、`README.md`

- [ ] **Step 1: util 清理助手**

`tests/util.py` 加：

```python
def kill_runtimes_under(data_dir) -> None:
    """Terminate poly_sim_runtime processes whose config lives under data_dir."""
    import psutil
    from pathlib import Path

    root = Path(data_dir).resolve()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not any("poly_sim_runtime" in part for part in cmdline):
            continue
        config = next((cmdline[i + 1] for i, p in enumerate(cmdline) if p == "--config" and i + 1 < len(cmdline)), "")
        try:
            if config and Path(config).resolve().is_relative_to(root):
                proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
            continue
```

- [ ] **Step 2: fixture teardown 调用**

在 `tests/test_client_connections.py` 与 `tests/test_creation_defaults_api.py` 的 `api` fixture `finally` 里，`process.terminate()` 之后加 `kill_runtimes_under(data)`（import 自 `.util`）。

- [ ] **Step 3: 文档**

`AGENTS.md` 约定补：reset 保持连接清零、仅 sim；maintenance 只杀测试残留。`README.md` 补一句重置/清理说明。

- [ ] **Step 4: 全量验证**

Run: `uv run pytest` → 全绿。
Run: `npm --prefix apps/web run typecheck` / `check:address` / `build` → 全过。
Run: `git diff --check` → 无输出。
验证跑完无新增孤儿：`psutil` 断言 `pytest-of` 下无 poly_sim_runtime（可用 `kill_runtimes_under` 后再 list）。

- [ ] **Step 5: 提交**

```bash
git add tests/util.py tests/test_client_connections.py tests/test_creation_defaults_api.py AGENTS.md README.md
git commit -m "fix: stop test runtimes on teardown; document reset/cleanup" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 计划自检

- **Spec coverage**：reset（Task 1-3）、maintenance 仅测试残留（Task 4）、前端（Task 5）、夹具根因+文档（Task 6）。安全约束（owned/非 test 不杀）有测试。
- **Placeholder scan**：无 TBD/TODO；代码步骤均给完整片段。
- **Type consistency**：`reset()` 全链路同名；`list_runtimes`/`kill_test_runtimes` 签名一致；`RuntimeProcess` 字段与后端返回一致；`is_test`/`owned` 判定一致。
