# 审计增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让管理员在 `/audit` 页面看到「谁在什么时候做了什么」，覆盖增删改与地址值写入；审计保留 1000 条（`POLY_AUDIT_KEEP` 可配），所有写操作带结构化 params，并为 AI 创建预留 `source`。

**Architecture:** 后端 `_audit()` 保留策略改为可配；补全各写操作的审计点与 `changed` diff；`/api/audit` 支持 actor/action/q 过滤、limit/offset 分页、返回总数并收紧为管理员；新增前端 `AuditPage`（5s 轮询、加载更多、中文动作映射、AI 徽标）。`server.create` 契约新增 `source`（默认 `manual`）。

**Tech Stack:** Python 3.10+、uv、FastAPI、Pydantic v2、SQLModel/SQLite、React 19、TS、Vite、zustand。

---

## 文件结构

- `apps/control-plane/poly_control_plane/app.py`：`_audit` 保留策略、各审计点、`/api/audit`。
- `packages/contracts/poly_contracts/models.py`：`ServerCreate.source`。
- `apps/web/src/api.ts`：`audit(params)` 返回 `{items,total}`。
- `apps/web/src/pages/AuditPage.tsx`（新）：审计页面。
- `apps/web/src/App.tsx`：`/audit` 路由 + 侧边栏入口（admin）。
- `apps/web/src/styles.css`：少量样式。
- `tests/test_audit_enhancement.py`（新）：保留策略、审计点、API 过滤/分页/权限。

---

### Task 1: 审计保留策略可配

**Files:**
- Modify: `apps/control-plane/poly_control_plane/app.py`
- Create: `tests/test_audit_enhancement.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_audit_enhancement.py`（自带模块级 `api` fixture，参照 `tests/test_client_connections.py`）：

```python
"""Audit retention, audit points, and the audit API."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
import pytest

from poly_control_plane.system import ROOT
from tests.conftest import free_port


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    data = tmp_path_factory.mktemp("poly-audit-api")
    port = free_port()
    env = {**os.environ, "POLY_DATA_DIR": str(data), "POLY_AUDIT_KEEP": "5"}
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


def test_audit_is_trimmed_to_configured_keep(api) -> None:
    for i in range(8):
        assert api.post("/api/sites", json={"name": f"s{i}", "host": ""}).status_code == 201
    audit = api.get("/api/audit?limit=500").json()
    assert audit["total"] <= 5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_audit_enhancement.py::test_audit_is_trimmed_to_configured_keep -v`
Expected: FAIL（当前保留 100，`total` 为 8）。

- [ ] **Step 3: 实现保留策略**

`apps/control-plane/poly_control_plane/app.py` 的 `_audit()` 中，把 `limit(100)` 改为可配：

```python
def _audit_keep() -> int:
    try:
        keep = int(os.environ.get("POLY_AUDIT_KEEP", "1000"))
    except ValueError:
        keep = 1000
    return max(100, keep)
```

并把 trim 里的 `limit(100)` 改为 `limit(_audit_keep())`。注释同步为「默认 1000 条，可用 `POLY_AUDIT_KEEP` 调整」。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_audit_enhancement.py::test_audit_is_trimmed_to_configured_keep -v`
Expected: 1 passed。

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/poly_control_plane/app.py tests/test_audit_enhancement.py
git commit -m "feat: configurable audit retention" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: 补全审计点与结构化 params

**Files:**
- Modify: `packages/contracts/poly_contracts/models.py`
- Modify: `apps/control-plane/poly_control_plane/app.py`
- Modify: `tests/test_audit_enhancement.py`

- [ ] **Step 1: 契约加 source**

`packages/contracts/poly_contracts/models.py` 的 `ServerCreate` 加：

```python
    # "manual" (页面创建) or "ai" (AI 点表创建，后续 spec)。
    source: str = "manual"
```

- [ ] **Step 2: 写失败测试**

在 `tests/test_audit_enhancement.py` 追加：

```python
def _sim(api, protocol="modbus_tcp"):
    site = api.post("/api/sites", json={"name": "a-site", "host": ""}).json()
    r = api.post(f"/api/sites/{site['id']}/servers",
                 json={"name": "a", "protocol": protocol, "port": free_port()})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_audit_points_record_changes(api) -> None:
    site = api.post("/api/sites", json={"name": "a-site", "host": ""}).json()

    # site.update 记录真实变更字段
    api.patch(f"/api/sites/{site['id']}", json={"name": "renamed", "description": "d"})
    audit = api.get("/api/audit?action=site.update").json()["items"]
    assert audit[0]["params"]["changed"] == {"name": ["a-site", "renamed"], "description": ["", "d"]}

    # server.create 带 source / tags_count
    server_id = _sim(api)
    audit = api.get("/api/audit?action=server.create").json()["items"]
    assert audit[0]["params"]["source"] == "manual"
    assert audit[0]["params"]["tags_count"] == 0

    # server.update 记录真实变更
    api.patch(f"/api/servers/{server_id}", json={"name": "b", "port": free_port()})
    audit = api.get("/api/audit?action=server.update").json()["items"]
    assert "name" in audit[0]["params"]["changed"]
    assert "port" in audit[0]["params"]["changed"]

    # restart 有审计
    api.post(f"/api/servers/{server_id}/restart")
    assert api.get("/api/audit?action=server.restart").json()["total"] >= 1

    # area.write 带 area/address/value/link
    api.post(f"/api/servers/{server_id}/area", json={"area": "holding", "address": 2, "value": 7})
    audit = api.get("/api/audit?action=area.write").json()["items"]
    assert audit[0]["params"] == {"area": "holding", "address": 2, "value": 7, "link": False}

    # server.delete 带摘要
    api.delete(f"/api/servers/{server_id}")
    audit = api.get("/api/audit?action=server.delete").json()["items"]
    assert audit[0]["params"]["name"] == "b"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/test_audit_enhancement.py::test_audit_points_record_changes -v`
Expected: FAIL（params 为空/缺字段）。

- [ ] **Step 4: 实现 diff 与各审计点**

`app.py` 顶部 helpers 区加：

```python
def _changed(old: dict, new: dict, fields: tuple[str, ...]) -> dict[str, list]:
    return {f: [old[f], new[f]] for f in fields if old.get(f) != new.get(f)}
```

`site.update`：在函数开头捕获旧值：

```python
    old = {"name": row.name, "description": row.description, "host": row.host,
           "ssh_port": row.ssh_port, "ssh_user": row.ssh_user, "deploy_dir": row.deploy_dir}
```

修改赋值后，在 `_audit` 处：

```python
    _audit(
        session,
        "site.update",
        site_id,
        f"{row.name} {row.host or '本机'}",
        params={"changed": _changed(old, {
            "name": row.name, "description": row.description, "host": row.host,
            "ssh_port": row.ssh_port, "ssh_user": row.ssh_user, "deploy_dir": row.deploy_dir,
        }, ("name", "description", "host", "ssh_port", "ssh_user", "deploy_dir"))},
    )
```

`site.delete`：在 `session.delete(row)` 前捕获：

```python
    server_count = len(session.exec(select(ServerRow).where(ServerRow.site_id == site_id)).all())
    _audit(
        session, "site.delete", site_id, f"{row.name} {row.host}",
        params={"name": row.name, "host": row.host, "server_count": server_count},
    )
```

`server.create` 的 params 增补：

```python
            "bind_ip": row.bind_ip,
            "tags_count": len(tags),
            "source": body.source,
```

`server.update`：在函数开头捕获旧值：

```python
    old = {"name": row.name, "bind_ip": row.bind_ip, "port": row.port, "enabled": row.enabled,
           "options": row.options, "target_host": row.target_host,
           "target_port": row.target_port, "target_options": row.target_options}
```

在 `_audit` 处改为：

```python
    _audit(
        session,
        "server.update",
        row.id,
        f"v{row.config_version}",
        params={
            "changed": _changed(old, {
                "name": row.name, "bind_ip": row.bind_ip, "port": row.port, "enabled": row.enabled,
                "options": row.options, "target_host": row.target_host,
                "target_port": row.target_port, "target_options": row.target_options,
            }, ("name", "bind_ip", "port", "enabled", "options", "target_host", "target_port", "target_options")),
            "config_version": row.config_version,
        },
    )
```

`server.delete`：在 `session.delete(row)` 前：

```python
    _audit(
        session, "server.delete", server_id,
        f"{row.name} {row.protocol} {row.kind} {row.port}",
        params={"name": row.name, "protocol": row.protocol, "kind": row.kind, "port": row.port},
    )
```

`restart_server`：在 `_launch` 前加：

```python
    _audit(session, "server.restart", server_id, row.name, params={"name": row.name})
    session.commit()
```

`tags.replace` 的 params：

```python
    _audit(
        session, "tags.replace", server_id, f"{len(tags)} tags, v{row.config_version}",
        params={"count": len(tags), "names": [tag.name for tag in tags[:50]]},
    )
```

`area.write` 的 params：

```python
    _audit(
        session, "area.write", f"{server_id}/{body.area}:{body.address}",
        str(body.value) + (" (link)" if row.is_link else ""),
        params={"area": body.area, "address": body.address, "value": body.value, "link": row.is_link},
    )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_audit_enhancement.py -v`
Expected: 2 passed。

- [ ] **Step 6: 提交**

```bash
git add packages/contracts/poly_contracts/models.py apps/control-plane/poly_control_plane/app.py tests/test_audit_enhancement.py
git commit -m "feat: record structured params on all audit points" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: `/api/audit` 过滤、分页、总数、管理员

**Files:**
- Modify: `apps/control-plane/poly_control_plane/app.py`
- Modify: `tests/test_audit_enhancement.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_audit_enhancement.py` 追加：

```python
def test_audit_api_filters_pagination_and_permission(api) -> None:
    site = api.post("/api/sites", json={"name": "f-site", "host": ""}).json()
    api.patch(f"/api/sites/{site['id']}", json={"name": "f-renamed"})

    # 过滤 + 总数
    filtered = api.get("/api/audit?action=site.update").json()
    assert filtered["total"] >= 1
    assert all(item["action"] == "site.update" for item in filtered["items"])

    # 关键字
    by_q = api.get("/api/audit?q=nonexistent-xyz").json()
    assert by_q["total"] == 0

    # 分页
    page = api.get("/api/audit?limit=1&offset=0").json()
    assert len(page["items"]) == 1

    # 非管理员 403
    api.post("/api/users", json={"username": "plain"})
    api.cookies.clear()
    api.post("/api/login", json={"username": "plain", "password": "123456"})
    assert api.get("/api/audit").status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_audit_enhancement.py::test_audit_api_filters_pagination_and_permission -v`
Expected: FAIL（返回 list，无 total；权限 200）。

- [ ] **Step 3: 实现**

`apps/control-plane/poly_control_plane/app.py` 用下面的实现替换 `list_audit`（保持路由与 `GET /api/audit`）：

```python
@app.get("/api/audit")
def list_audit(
    actor: str = Query(default=""),
    action: str = Query(default=""),
    q: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: UserRow = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    conditions = []
    if actor:
        conditions.append(AuditRow.actor.ilike(f"%{actor}%"))
    if action:
        if action.endswith("."):
            conditions.append(AuditRow.action.like(f"{action}%"))
        else:
            conditions.append(AuditRow.action == action)
    if q:
        conditions.append(AuditRow.target.ilike(f"%{q}%") | AuditRow.detail.ilike(f"%{q}%"))
    total = session.exec(
        select(func.count()).select_from(AuditRow).where(*conditions)
    ).one()
    rows = session.exec(
        select(AuditRow).where(*conditions).order_by(AuditRow.id.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "created_at": row.created_at.isoformat(),
                "actor": row.actor,
                "action": row.action,
                "target": row.target,
                "detail": row.detail,
                "params": row.params,
            }
            for row in rows
        ],
        "total": total,
    }
```

顶部 import 增加 `func`：`from sqlmodel import Session, SQLModel, delete, func, select`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_audit_enhancement.py -v`
Expected: 3 passed。

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/poly_control_plane/app.py tests/test_audit_enhancement.py
git commit -m "feat: filter, paginate, and restrict audit API to admins" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: 前端审计页

**Files:**
- Modify: `apps/web/src/api.ts`
- Create: `apps/web/src/pages/AuditPage.tsx`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/styles.css`

- [ ] **Step 1: API 改为 items/total**

`apps/web/src/api.ts` 的 `audit` 改为：

```ts
  audit: (params: { actor?: string; action?: string; q?: string; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams()
    if (params.actor) query.set('actor', params.actor)
    if (params.action) query.set('action', params.action)
    if (params.q) query.set('q', params.q)
    if (params.limit) query.set('limit', String(params.limit))
    if (params.offset) query.set('offset', String(params.offset))
    const qs = query.toString()
    return request<{ items: AuditEntry[]; total: number }>(`/api/audit${qs ? `?${qs}` : ''}`)
  },
```

- [ ] **Step 2: AuditPage**

创建 `apps/web/src/pages/AuditPage.tsx`：过滤器（actor 文本、action 下拉、q 文本），表格列 时间/操作人/动作/对象/详情/参数；`usePolling(fn, 5000, true)` 每 5 秒刷新第一页；底部「加载更多」用 `offset += limit`；`params.source === 'ai'` 显示「AI」徽标；参数默认摘要（`port: 15020 → 15021`、`area: holding 0 = 1234`），可展开完整 JSON。动作中文映射至少覆盖：site.create/update/delete、server.create/update/delete/restart/reset、tags.replace、area.write、tag.write、user.create/delete/password_change/login/logout、maintenance.kill、server.client_kick。时间用 `new Date(value).toLocaleString()`。空状态「没有匹配的审计记录」；加载失败 toast/错误行。样式复用 `.conn-table`/`.conn-row` 或新增少量类。

- [ ] **Step 3: 路由与入口**

`App.tsx`：加 `import { AuditPage } from './pages/AuditPage'`；`Routes` 加 `<Route path="/audit" element={<AuditPage />} />`；侧边栏在「全部实例」下方加「审计」入口，仅 `user.is_admin` 可见，图标 `clock`，`NavLink to="/audit"`。

- [ ] **Step 4: 验证**

Run: `npm --prefix apps/web run typecheck` → exit 0。
Run: `npm --prefix apps/web run build` → built。

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/api.ts apps/web/src/pages/AuditPage.tsx apps/web/src/App.tsx apps/web/src/styles.css
git commit -m "feat: admin audit page with filters and load-more" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: 文档与全量验证

**Files:**
- Modify: `AGENTS.md`、`README.md`

- [ ] **Step 1: 文档**

`AGENTS.md` 约定补：审计默认保留 1000 条（`POLY_AUDIT_KEEP` 可配）；`/audit` 仅管理员；`server.create` 的 `source` 预留 `manual|ai`。`README.md` 补：审计页面位置与保留策略。

- [ ] **Step 2: 全量验证**

Run: `uv run pytest` → 全绿。
Run: `npm --prefix apps/web run typecheck` / `check:address` / `build` → 全过。
Run: `git diff --check` → 无输出。

- [ ] **Step 3: 手动走查**

管理员登录 → 侧边栏「审计」→ 过滤/加载更多；非管理员确认入口不可见且接口 403。

- [ ] **Step 4: 提交**

```bash
git add AGENTS.md README.md
git commit -m "docs: document audit retention and admin audit page" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 计划自检

- **Spec coverage**：保留策略（Task 1）、审计点与 params（Task 2）、API 过滤分页权限（Task 3）、前端页面（Task 4）、文档验证（Task 5）。`source` 预留已纳入 Task 2。
- **Placeholder scan**：无 TBD/TODO；代码步骤给完整片段。
- **Type consistency**：`/api/audit` 返回 `{items,total}` 前后端一致；`ServerCreate.source` 全链路同名；`POLY_AUDIT_KEEP` 命名一致。
