# 可信基线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前未提交的登录 WIP 收口成可信基线，并让创建实例默认值、文案和摘要符合工控联调场景。

**Architecture:** 后端沿用 FastAPI Cookie 中间件、SQLModel/SQLite、现有 `ServerCreate` 契约与 `create_server` 流程；在用户表加 `token_version`，让 Fernet token 随改密失效，并把 `sample_tags` 默认改为 false。前端沿用 zustand、现有 `Modal` 和 `CreateServerModal`，补默认密码提示、改密入口、高级设置折叠和创建摘要。不引入新依赖、新表（除用户表补列）、新 provider 或协议改动。

**Tech Stack:** Python 3.10+、uv、FastAPI、Pydantic v2、SQLModel、SQLite、pytest/httpx、React 19、TypeScript、Vite、zustand、手写 CSS token。

---

## 文件结构

- `apps/control-plane/poly_control_plane/auth.py`：密码约束、token 载荷/解析、登录响应、当前用户校验。
- `apps/control-plane/poly_control_plane/db.py`：新增 `UserRow.token_version`。
- `apps/control-plane/poly_control_plane/migrate.py`：旧库补 `user.token_version`。
- `apps/control-plane/poly_control_plane/app.py`：修正 `Response` 注解，登录传 token version，中间件校验 token version，改密递增版本，补创建默认值 API 测试所需行为。
- `apps/control-plane/poly_control_plane/__main__.py`：保持幂等 `--init-admin`，明确输出默认密码提醒。
- `packages/contracts/poly_contracts/models.py`：`ServerCreate.sample_tags` 默认改 false。
- `tests/test_auth.py`：环境隔离、密码约束、token 失效、两级权限回归。
- `tests/test_creation_defaults.py`：创建默认值 API 回归（新文件）。
- `apps/web/src/App.tsx`：默认密码横幅、用户菜单改密入口、退出登录失败兜底。
- `apps/web/src/components/PasswordChangeModal.tsx`：本人改密弹窗（新文件）。
- `apps/web/src/components/UserManageModal.tsx`：复用 `PasswordChangeModal` 的管理员改密弹窗，密码长度校验。
- `apps/web/src/components/CreateServerModal.tsx`：演示模板默认关闭、高级设置折叠、创建摘要、文案。
- `apps/web/src/store.ts`：401 清理用户态、退出登录兜底、改密后刷新 `user`。
- `apps/web/src/api.ts`：401 统一抛错（不新增接口）。
- `apps/web/src/types.ts`：不改协议类型；保留 `User` / `AuditEntry`。
- `apps/web/src/styles.css`：横幅、高级设置、摘要、改密弹窗样式，兼容深浅色。
- `apps/web/src/pages/ServerPage.tsx` 与 `apps/web/src/components/ValueTable.tsx`：link 空标签表引导文案，跳转标签表。
- `README.md`：启动命令、登录边界、创建默认值说明。
- `AGENTS.md`、`docs/architecture.md`：同步「已实现登录」「两级权限」「默认空地址空间」口径。

当前工作树已有 12 个修改文件和 4 个新增文件，属于同一登录 WIP。执行时保留这些改动，所有 `git add` / `git commit` 步骤只列本任务相关文件，不提交用户未授权的其它内容；每个提交都追加 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

---

### Task 1: 创建默认值契约与后端回归

**Files:**
- Modify: `packages/contracts/poly_contracts/models.py:280-297`
- Create: `tests/test_creation_defaults.py`
- Modify: `tests/conftest.py`（若复用 `free_port` / `make_config` 无需改）

- [ ] **Step 1: 写失败测试**

```python
"""Creation defaults must not invent device data."""

from __future__ import annotations

from poly_contracts import Protocol, ServerCreate


def test_sample_tags_default_is_false() -> None:
    body = ServerCreate(name="s7-empty", protocol=Protocol.S7, port=15102)
    assert body.sample_tags is False


def test_explicit_sample_tags_true_still_works() -> None:
    body = ServerCreate(name="s7-demo", protocol=Protocol.S7, port=15102, sample_tags=True)
    assert body.sample_tags is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_creation_defaults.py -v`  
Expected: `test_sample_tags_default_is_false` FAIL，实际默认值为 `True`。

- [ ] **Step 3: 修改契约默认值**

```python
class ServerCreate(BaseModel):
    # ...
    # Explicit tags win. With no tags, sample_tags=True seeds the demo table;
    # False creates an all-zero data plane, which is what most test clients want.
    sample_tags: bool = False
```

同步把注释上一行的「False creates an all-zero data plane」改成「False creates an empty address space for tag-based protocols / all-zero raw areas for Modbus」。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_creation_defaults.py -v`  
Expected: 2 passed。

- [ ] **Step 5: 提交**

```bash
git add packages/contracts/poly_contracts/models.py tests/test_creation_defaults.py
git commit -m "fix: default creation to empty address space" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: 登录基础修复与环境隔离

**Files:**
- Modify: `apps/control-plane/poly_control_plane/app.py:12-15,438-458`
- Modify: `tests/test_auth.py:1-70`
- Modify: `apps/control-plane/poly_control_plane/__main__.py:10-43`
- Modify: `README.md:44-94,384-400`

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth.py` 顶部加入：

```python
import os
```

把 fixture 的 env 行改成：

```python
    env = {**os.environ, "POLY_DATA_DIR": str(data)}
```

把健康检查改成显式忽略代理：

```python
                if httpx.get(
                    f"{base}/api/health", timeout=1.0, trust_env=False
                ).status_code == 200:
                    break
```

追加：

```python
def test_login_route_has_resolved_response_annotation() -> None:
    from poly_control_plane import app as app_module

    route = next(r for r in app_module.app.routes if getattr(r, "path", "") == "/api/login")
    assert route.response_model is None
    assert app_module.login.__annotations__.get("return") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_auth.py::test_login_route_has_resolved_response_annotation -v`  
Expected: FAIL，当前 `response_model` 是 `ForwardRef('Response')`，`login.__annotations__['return']` 是字符串 `Response`。

- [ ] **Step 3: 修正 Response 注解与启动日志**

`app.py` 保持 `from fastapi.responses import FileResponse, JSONResponse`，把两个端点签名改成无返回注解：

```python
@app.post("/api/login")
def login(body: LoginBody, session: Session = Depends(get_session)):
```

```python
@app.post("/api/logout")
def logout(session: Session = Depends(get_session)):
```

`__main__.py` 的 `_init_admin` 在创建管理员时保留输出：

```python
        print(
            f"已创建管理员「{row.username}」（默认密码 {DEFAULT_PASSWORD}，请尽快修改）"
        )
```

把 `--init-admin` 的 help 明确写成：

```python
        help="create an admin user (default password 123456) if no users exist, then start",
```

`README.md` 的单端口启动命令改为：

```powershell
uv run python -m poly_control_plane --init-admin admin1 --host 0.0.0.0 --port 25011
```

前后端分离启动命令已经带 `--init-admin admin1`，保持不变。删除限制段的「没有登录鉴权、没有多用户；不要直接暴露到公网。」，替换为：

```markdown
- 已有登录与两级权限：登录用户可操作实例，管理员额外管理用户。Cookie 为 HttpOnly、8 小时有效；
  当前控制面默认走明文 HTTP，仅适合可信局域网，不要直接暴露到公网。首次管理员默认密码是 `123456`，
  登录后请立即修改；改密会让此前签发的 Cookie 失效。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_auth.py::test_login_route_has_resolved_response_annotation -v`  
Expected: PASS。  
Run: `uv run pytest tests/test_auth.py -v`  
Expected: 现有 11 个测试 + 本测试全部通过。

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/poly_control_plane/app.py apps/control-plane/poly_control_plane/__main__.py tests/test_auth.py README.md
git commit -m "fix: close auth startup and annotation gaps" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: token_version 与改密后旧会话失效

**Files:**
- Modify: `apps/control-plane/poly_control_plane/db.py:101-112`
- Modify: `apps/control-plane/poly_control_plane/migrate.py:15-42`
- Modify: `apps/control-plane/poly_control_plane/auth.py:7-10,66-116,135-147`
- Modify: `apps/control-plane/poly_control_plane/app.py:415-435,438-451,516-534`
- Modify: `tests/test_auth.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth.py` 追加：

```python
def test_old_cookie_is_rejected_after_password_change(auth_server: httpx.Client) -> None:
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})
    old_cookie = auth_server.cookies.get("poly_auth")
    assert old_cookie

    me = auth_server.get("/api/me").json()
    response = auth_server.post(
        f"/api/users/{me['id']}/password", json={"password": "admin-new-pass"}
    )
    assert response.status_code == 200

    auth_server.cookies.set("poly_auth", old_cookie)
    assert auth_server.get("/api/sites").status_code == 401

    auth_server.cookies.clear()
    assert auth_server.post(
        "/api/login", json={"username": "admin1", "password": "admin-new-pass"}
    ).status_code == 204
    assert auth_server.get("/api/sites").status_code == 200

    # Restore the shared module fixture's password for later tests.
    restore = auth_server.post(
        f"/api/users/{me['id']}/password", json={"password": "123456"}
    )
    assert restore.status_code == 200
```

本测试必须放在 `test_auth.py` 文件末尾，并在末尾恢复 `admin1 / 123456`。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_auth.py::test_old_cookie_is_rejected_after_password_change -v`  
Expected: FAIL，当前旧 Cookie 在改密后仍返回 200。

- [ ] **Step 3: 写最小实现**

`db.py` 的 `UserRow` 增加字段：

```python
    # Bumped on every password change; Fernet tokens carry this version.
    token_version: int = 0
```

`migrate.py` 的 `EXPECTED_COLUMNS` 增加：

```python
    "user": {
        "token_version": "INTEGER DEFAULT 0",
    },
```

`auth.py` 模块文档把 token 载荷描述改为：

```python
Token = Fernet-encrypted JSON ``{"u": username, "ver": token_version, "exp": ts}``.
```

把 `_make_token` / `_parse_token` / `login_response` / `current_user` 改为：

```python
def _make_token(username: str, token_version: int) -> str:
    payload = json.dumps(
        {"u": username, "ver": token_version, "exp": int(time.time()) + COOKIE_MAX_AGE}
    )
    return secret_store.encrypt_secret(data_dir(), payload)
```

```python
def _parse_token(token: str) -> tuple[str, int] | None:
    if not token:
        return None
    try:
        payload = secret_store.decrypt_secret(data_dir(), token)
        if not payload:
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        username = data.get("u")
        if not isinstance(username, str):
            return None
        version = data.get("ver", 0)
        return username, int(version)
    except Exception:
        return None
```

```python
def login_response(username: str, token_version: int) -> Response:
    resp = Response(status_code=204)
    resp.set_cookie(
        COOKIE_NAME,
        _make_token(username, token_version),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=False,
    )
    return resp
```

```python
def current_user(request: Request, session: Session = Depends(get_session)) -> UserRow:
    parsed = _parse_token(request.cookies.get(COOKIE_NAME))
    if parsed is None:
        raise HTTPException(status_code=401, detail="未登录")
    username, version = parsed
    row = session.exec(select(UserRow).where(UserRow.username == username)).first()
    if row is None or row.token_version != version:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return row
```

`auth.py` 的请求体改为：

```python
from pydantic import BaseModel, Field


class PasswordChangeBody(BaseModel):
    password: str = Field(min_length=8, max_length=128)
```

`app.py` 的 `_auth_gate` 在解析 token 后加一次 DB 版本校验：

```python
    parsed = _parse_token(request.cookies.get(COOKIE_NAME))
    if parsed is None:
        return JSONResponse(status_code=401, content={"detail": "未登录"})
    username, version = parsed
    with Session(engine) as session:
        row = session.exec(select(UserRow).where(UserRow.username == username)).first()
        if row is None or row.token_version != version:
            return JSONResponse(status_code=401, content={"detail": "登录已失效，请重新登录"})
    token = _current_user.set(username)
```

`login` 改为：

```python
    return login_response(row.username, row.token_version)
```

`change_password` 在设置密码哈希后增加：

```python
    row.token_version += 1
```

并把 `create_user` / `_init_admin` 里新建 `UserRow` 的字段保持默认 `token_version=0`。`delete_user` 不需要递增版本；用户不存在时 `_auth_gate` 返回 401。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_auth.py -v`  
Expected: 全部通过，包含改密后旧 Cookie 401。  
Run: `uv run pytest tests/test_link.py -v`  
Expected: 28 个 HTTP 端到端测试通过（fixture 登录拿到的 token version 为 0）。

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/poly_control_plane/db.py apps/control-plane/poly_control_plane/migrate.py apps/control-plane/poly_control_plane/auth.py apps/control-plane/poly_control_plane/app.py tests/test_auth.py
git commit -m "fix: invalidate stale cookies after password changes" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: 两级权限与密码约束回归

**Files:**
- Modify: `tests/test_auth.py`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/components/PasswordChangeModal.tsx`（Create）
- Modify: `apps/web/src/components/UserManageModal.tsx`
- Modify: `apps/web/src/styles.css`

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth.py` 追加：

```python
def test_short_password_is_rejected(auth_server: httpx.Client) -> None:
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})
    me = auth_server.get("/api/me").json()
    response = auth_server.post(
        f"/api/users/{me['id']}/password", json={"password": "short"}
    )
    assert response.status_code == 422
    assert auth_server.get("/api/me").json()["must_change_password"] is True


def test_regular_user_can_read_instances_but_not_manage_users(auth_server: httpx.Client) -> None:
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})
    auth_server.post("/api/users", json={"username": "operator1"})
    auth_server.cookies.clear()
    assert auth_server.post(
        "/api/login", json={"username": "operator1", "password": "123456"}
    ).status_code == 204
    assert auth_server.get("/api/sites").status_code == 200
    assert auth_server.get("/api/users").status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_auth.py::test_short_password_is_rejected -v`  
Expected: FAIL，当前 5 字符密码返回 200。  
Run: `uv run pytest tests/test_auth.py::test_regular_user_can_read_instances_but_not_manage_users -v`  
Expected: PASS（该测试守卫现有两级权限，若已有实现正确则无需改后端）。

- [ ] **Step 3: 写本人改密弹窗并接菜单**

新建 `apps/web/src/components/PasswordChangeModal.tsx`：

```tsx
import { useState } from 'react'
import { Modal } from './Modal'
import { useApp } from '../store'

interface Props {
  open: boolean
  userId: string
  title?: string
  onClose: () => void
}

export function PasswordChangeModal({ open, userId, title = '修改密码', onClose }: Props) {
  const changePassword = useApp((state) => state.changePassword)
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (password.length < 8 || password.length > 128) {
      setError('新密码需为 8–128 个字符')
      return
    }
    setBusy(true)
    setError('')
    try {
      await changePassword(userId, password)
      setPassword('')
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} title={title} width={400} onClose={onClose} footer={
      <>
        <button type="button" className="btn ghost" onClick={onClose}>取消</button>
        <button type="button" className="btn primary" disabled={busy || !password} onClick={() => void submit()}>
          {busy ? '保存中…' : '确认'}
        </button>
      </>
    }>
      <label className="field">
        <span>新密码</span>
        <input
          id="password-change-input"
          type="password"
          minLength={8}
          maxLength={128}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              void submit()
            }
          }}
          autoFocus
        />
        <small className="hint">8–128 个字符。保存后旧登录 Cookie 会失效。</small>
      </label>
      {error ? <p className="login-error">{error}</p> : null}
    </Modal>
  )
}
```

`App.tsx` 的 `Sidebar` 导入 `PasswordChangeModal`，新增：

```tsx
  const [passwordOpen, setPasswordOpen] = useState(false)
```

在用户菜单中，`用户管理` 后、`退出登录` 前加入：

```tsx
                  <button
                    type="button"
                    onClick={() => {
                      setPasswordOpen(true)
                      setUserMenuOpen(false)
                    }}
                  >
                    <Icon name="key" size={14} />
                    <span>修改密码</span>
                  </button>
```

在 `side-foot` 的 `UserManageModal` 后渲染：

```tsx
        {user ? (
          <PasswordChangeModal
            open={passwordOpen}
            userId={user.id}
            onClose={() => setPasswordOpen(false)}
          />
        ) : null}
```

`UserManageModal.tsx` 删除 `newPassword` / `changingFor` / `handleChangePassword` 及第二个 `Modal`，改为：

```tsx
import { PasswordChangeModal } from './PasswordChangeModal'
```

```tsx
  const [changingFor, setChangingFor] = useState<User | null>(null)
```

改密按钮保留 `setChangingFor(user)`；在组件返回的片段末尾增加：

```tsx
      <PasswordChangeModal
        open={!!changingFor}
        userId={changingFor?.id ?? ''}
        title={`修改密码：${changingFor?.username ?? ''}`}
        onClose={() => setChangingFor(null)}
      />
```

把管理员弹窗里创建用户按钮的 `busy` 状态保留；删除 `handleChangePassword` 后不再引用 `newPassword`。`PasswordChangeModal` 使用现有 `Modal` 的 `field` / `login-error` / `btn` 样式，不新增 CSS。

- [ ] **Step 4: 跑验证**

Run: `uv run pytest tests/test_auth.py -v`  
Expected: 全部通过。  
Run: `npm --prefix apps/web run typecheck`  
Expected: exit 0。

- [ ] **Step 5: 提交**

```bash
git add tests/test_auth.py apps/web/src/App.tsx apps/web/src/components/PasswordChangeModal.tsx apps/web/src/components/UserManageModal.tsx
git commit -m "feat: add self-service password change" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: 默认密码横幅与 401 状态收口

**Files:**
- Modify: `apps/web/src/App.tsx:17-41,102-170,178-212`
- Modify: `apps/web/src/api.ts:22-43`
- Modify: `apps/web/src/store.ts:117-133`
- Modify: `apps/web/src/styles.css:3547-3712`
- Modify: `README.md:384-400`

- [ ] **Step 1: 写前端验证基线**

本轮没有前端组件测试框架；用 TypeScript 类型检查和生产构建作为验证基线。先执行：

Run: `npm --prefix apps/web run typecheck`  
Expected: exit 0（确认修改前基线）。

- [ ] **Step 2: 401 时清理用户态**

`api.ts` 的 `request<T>` 在 `if (!response.ok)` 内、解析消息之后、`throw` 之前加入：

```ts
    if (response.status === 401 && !path.startsWith('/api/login')) {
      try {
        const { useApp } = await import('./store')
        useApp.setState({ user: null, users: [] })
      } catch {
        // The login page can still fail before the store module is loaded.
      }
    }
```

把 `request` 改成 `async function request<T>` 已是现状；不要改成同步函数。`store.ts` 的 `logout` 改为：

```ts
  logout: async () => {
    try {
      await api.logout()
    } finally {
      set({ user: null, users: [] })
    }
  },
```

- [ ] **Step 3: 加入一次性默认密码横幅**

`App.tsx` 增加常量与状态：

```tsx
const PASSWORD_NOTICE_KEY = 'poly.passwordNotice.dismissed'
```

```tsx
  const [passwordNotice, setPasswordNotice] = useState(
    () => localStorage.getItem(PASSWORD_NOTICE_KEY) !== '1',
  )
```

在 `App` 的 shell 内、`<Sidebar>` 后、`<main>` 前插入：

```tsx
                {user?.must_change_password && passwordNotice ? (
                  <div className="password-notice" role="status">
                    <Icon name="key" size={16} />
                    <span>当前账号仍使用默认密码，请在用户菜单中尽快修改。</span>
                    <button
                      type="button"
                      className="icon-btn"
                      aria-label="关闭提醒"
                      onClick={() => {
                        localStorage.setItem(PASSWORD_NOTICE_KEY, '1')
                        setPasswordNotice(false)
                      }}
                    >
                      <Icon name="x" size={14} />
                    </button>
                  </div>
                ) : null}
```

`store.ts` 的 `changePassword` 在 `set(...)` 里同时更新 `user` 与 `users`（现状已更新），成功后调用 `get().loadMe()` 不必要；保持现状。`App.tsx` 在 `user` 变化时不需要额外 effect。

- [ ] **Step 4: 加样式**

在 `styles.css` 的用户样式段后加入：

```css
.password-notice {
  position: fixed;
  right: 24px;
  top: 20px;
  z-index: 60;
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: min(480px, calc(100vw - 48px));
  padding: 12px 14px;
  border: 1px solid var(--warn);
  border-radius: var(--radius-md, 12px);
  background: var(--surface-2, var(--surface));
  color: var(--text);
  box-shadow: var(--shadow-2, 0 8px 24px rgb(0 0 0 / 20%));
}

.password-notice span { flex: 1; }
```

检查现有 token：若 `--warn` / `--surface-2` / `--radius-md` / `--shadow-2` 不存在，改成项目实际存在的 `--warning` / `--surface` / `--radius` / `--shadow`；fallback 只用于新 token，不覆盖浅色主题。

- [ ] **Step 5: 跑验证并提交**

Run: `npm --prefix apps/web run typecheck`  
Expected: exit 0。  
Run: `npm --prefix apps/web run build`  
Expected: `✓ built in ...`，无 TypeScript error。

```bash
git add apps/web/src/App.tsx apps/web/src/api.ts apps/web/src/store.ts apps/web/src/styles.css README.md
git commit -m "feat: surface default password and clear stale auth state" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: 创建弹窗默认值、高级设置与摘要

**Files:**
- Modify: `apps/web/src/components/CreateServerModal.tsx:94-193,253-320,421-772`
- Modify: `apps/web/src/styles.css`

- [ ] **Step 1: 写后端回归，确保 API 省略 sample_tags 不生成变量**

在 `tests/test_creation_defaults.py` 追加契约序列化回归，避免依赖运行环境是否安装 OPC UA 适配器：

```python
def test_create_request_serializes_sample_tags_false() -> None:
    body = ServerCreate(name="x", protocol=Protocol.S7, port=15102)
    assert body.model_dump(exclude_none=True)["sample_tags"] is False
```

- [ ] **Step 2: 跑回归**

Run: `uv run pytest tests/test_creation_defaults.py -v`  
Expected: 3 passed。

- [ ] **Step 3: 前端默认值与文案改造**

`CreateServerModal.tsx` 增加：

```tsx
  const [advancedOpen, setAdvancedOpen] = useState(false)
```

`useEffect` 重置时：

```tsx
    setAdvancedOpen(false)
```

`chooseProtocol` 与 `chooseKind` 删除所有 `setSampleTags(true)`，统一改成：

```tsx
      setSampleTags(false)
```

`submit` 中 link 的 `sample_tags` 改为：

```tsx
          sample_tags: sampleTags,
```

自建 S7/OPC UA 的 `sample_tags` 保持 `sampleTags`；Modbus 仍为 false。把现有 `创建示例变量（温度 / 压力 / 计数器 / 运行状态）` 两处标签改成：

```tsx
                加载演示模板（4 个会变化的演示变量）
```

对应 hint 改成：

```tsx
                  仅用于冒烟测试，不是设备点表；默认不加载。不加载时为空地址空间，未定义变量。
```

link 分支的这段复选框移到「高级设置」内，默认隐藏；自建分支也放入高级设置。`registerCountText` 输入框标题改成「有效地址上限」，说明保留「四区共用、0–65535、留空等价 65536」。

- [ ] **Step 4: 高级设置折叠与创建摘要**

在 `CreateServerModal` 的 `return` JSX 内、错误框之前加入：

```tsx
      <details
        className="advanced-settings"
        open={advancedOpen || Boolean(portState && !portState.available)}
        onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
      >
        <summary>高级设置</summary>
        <div className="advanced-body">{/* 已存在的协议专属参数与演示模板字段 */}</div>
      </details>
```

把下列现有 JSX 块整体移入 `advanced-body`：link 的 `Rack/Slot/命名空间索引/超时` 行、link 演示模板复选框、自建的本机 `监听地址` 选择、`有效地址上限` 输入框、演示模板复选框、`Endpoint URL` 输入框。端口与目标地址保留在主区域。移动监听地址时保持现有 `remote ? null : (...)` 条件不变；移动到 `advanced-body` 后，`.row` 里保留「名称」和「端口」两个直接子元素。若 `portState?.available === false`，`open` 为 true，用户能看到端口预检结论。

在错误框前加入摘要：

```tsx
      <div className="create-summary" aria-live="polite">
        <span>{link ? '连接实例' : '自建实例'} · {PROTOCOL_META[protocol].label}</span>
        <span className="mono">
          {link
            ? `${targetHost.trim() || '?'}:${targetPortText.trim() || '?'}`
            : protocol === 'opcua'
              ? (endpointUrl.trim() || `opc.tcp://${remote ? site?.host ?? '目标机 IP' : bindIp === '0.0.0.0' ? (system?.addresses.find((ip) => ip !== '0.0.0.0' && ip !== '127.0.0.1') ?? '127.0.0.1') : bindIp}:${portText.trim()}`)
              : `${remote ? site?.host ?? '目标机 IP' : bindIp}:${portText.trim()}`}
        </span>
        <span>
          {protocol === 'modbus_tcp'
            ? `4 个原始数据区 · 每区 ${registerCountText.trim() || '65536'} 点 · 未写入任何值`
            : sampleTags
              ? '演示模板 4 个变量'
              : '0 个变量 · 空地址空间'}
        </span>
        <span className={portState?.available === false ? 'bad' : ''}>
          {portState ? (portState.available ? '端口预检可用' : '端口预检不可用') : '端口保存时复查'}
        </span>
      </div>
```

`styles.css` 增加 `.advanced-settings`、`.advanced-settings summary`、`.advanced-body`、`.create-summary` 规则，使用现有 `--surface` / `--muted` / `--line` / `--primary` / `--danger` token；浅色主题只靠 token 自动生效，不写死颜色。若现有 token 名不同，以 `styles.css:1-24` 的定义为准。

- [ ] **Step 5: 跑验证并提交**

Run: `npm --prefix apps/web run typecheck`  
Expected: exit 0。  
Run: `npm --prefix apps/web run build`  
Expected: built successfully。  
Run: `uv run pytest tests/test_creation_defaults.py -v`  
Expected: 3 passed。

```bash
git add apps/web/src/components/CreateServerModal.tsx apps/web/src/styles.css tests/test_creation_defaults.py
git commit -m "feat: professionalize instance creation defaults" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: link 空标签表引导

**Files:**
- Modify: `apps/web/src/components/ValueTable.tsx:145-158`
- Modify: `apps/web/src/pages/ServerPage.tsx:402-453`
- Modify: `apps/web/src/styles.css`

- [ ] **Step 1: 写前端基线**

Run: `npm --prefix apps/web run typecheck`  
Expected: exit 0（确认改动前基线）。

- [ ] **Step 2: 空状态文案按 link 分支**

`ValueTable.tsx` 把空表格行改为：

```tsx
          {server.tags.length === 0 ? (
            <tr>
              <td colSpan={5} className="table-empty">
                {link
                  ? '未定义变量，地址空间为空。去「标签表」添加对方的真实地址或 NodeId。'
                  : '未定义变量，地址空间为空。去「标签表」添加变量。'}
              </td>
            </tr>
          ) : null}
```

`ServerPage.tsx` 在 S7/OPC UA 的「实时变量」tab 内、`<ValueTable>` 上方，当 `link && current.tags.length === 0` 时渲染：

```tsx
              {link && current.tags.length === 0 ? (
                <div className="notice">
                  <Icon name="plug" size={15} />
                  <span>
                    连接实例没有原始数据区，变量表就是唯一视图。请在「标签表」填写对方真实地址或 NodeId；平台不会自动补演示变量。
                  </span>
                </div>
              ) : null}
```

`styles.css` 复用 `.notice`，不新增样式。

- [ ] **Step 3: 跑验证并提交**

Run: `npm --prefix apps/web run typecheck`  
Expected: exit 0。  
Run: `npm --prefix apps/web run build`  
Expected: built successfully。

```bash
git add apps/web/src/components/ValueTable.tsx apps/web/src/pages/ServerPage.tsx
git commit -m "feat: clarify empty link tag tables" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: 文档口径与全量回归

**Files:**
- Modify: `AGENTS.md:5-12,134-150,384-400`
- Modify: `docs/architecture.md:5-10,190-229`
- Modify: `README.md:98-152,384-400`

- [ ] **Step 1: 同步架构文档**

`AGENTS.md` 的「仓库现状」把「仍未实现：docker driver、登录权限、WS 推送」改成「仍未实现：docker driver、WS 推送；登录与两级权限已实现（当前工作树收口中）」。「命令」段的启动命令补 `--init-admin admin1`，把「正常退出会停掉它们」改成「正常退出只 detach，下次启动 adopt；只有 `--stop-on-exit` / `POLY_STOP_RUNTIMES_ON_EXIT=1` 才停」。测试数量从 60 更新为「按 `uv run pytest` 实际输出」。

`docs/architecture.md` §0 把「尚未实现：docker driver、登录与权限、WebSocket 推送」改成「尚未实现：docker driver、WebSocket 推送；登录与两级权限见 §11」。§11 API 形状追加：

```text
POST   /api/login                        Cookie 登录
POST   /api/logout                       退出登录
GET    /api/me                          当前用户
GET    /api/users                       管理员：用户列表
POST   /api/users                       管理员：创建用户（默认密码 123456）
DELETE /api/users/{id}                  管理员：删除用户
POST   /api/users/{id}/password         本人或管理员：修改密码，旧 Cookie 失效
```

`README.md` 使用流程补一句：S7 / OPC UA 默认创建空地址空间；「加载演示模板」仅用于冒烟测试；link 不会自动补变量。限制段补「Cookie 明文 HTTP 仅适合可信 LAN、HTTPS/CSRF 后续项」。

- [ ] **Step 2: 全量验证**

Run: `uv run pytest`  
Expected: 所有现有 81 个测试 + 新增 5 个回归全绿；输出 `passed`，无 failure/error。  
Run: `npm --prefix apps/web run typecheck`  
Expected: exit 0。  
Run: `npm --prefix apps/web run check:address`  
Expected: `全部通过（31 条）`。  
Run: `npm --prefix apps/web run build`  
Expected: `✓ built in ...`。  
Run: `git diff --check`  
Expected: 无输出。

- [ ] **Step 3: 临时数据目录启动走查**

用两个独立终端各执行一次（可用后台进程，结束即清理），不要使用仓库真实 `data/`：

```bash
POLY_DATA_DIR="$(mktemp -d)" uv run python -m poly_control_plane --init-admin admin1 --host 127.0.0.1 --port 25012
```

```bash
cd apps/web && npm run dev
```

Expected: 后端日志出现「已创建管理员…默认密码 123456」；浏览器访问 `http://127.0.0.1:5173/`，用 `admin1 / 123456` 登录，出现默认密码横幅；用户菜单有「修改密码」与「用户管理」；创建页默认没有演示变量、高级设置折叠、摘要显示连接地址；改密后刷新页面要求重新登录。走查后停止两个进程并删除临时目录。

- [ ] **Step 4: 提交文档与最终状态**

```bash
git add AGENTS.md docs/architecture.md README.md
git commit -m "docs: align trusted baseline behavior" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
git status --short
```

Expected: 只显示用户明确要求保留的其它未提交文件；本计划涉及的文件均已提交。

---

## 计划自检

- **Spec coverage:** 登录启动、README、Response 注解、测试环境、默认口令提示、密码长度、token_version、两级权限、创建默认值、演示模板、空地址空间、高级设置、摘要、link 空表引导、文档与全量验证均有对应任务。AI 导入、细粒度角色、静态初值、provider 配置、docker/WS/Postgres 明确不做。
- **Placeholder scan:** 计划中的「若 token 名不同以实际定义为准」不是待办，而是要求执行者使用项目真实 token；所有代码步骤都给了具体片段和命令。Task 6 的不稳定 HTTP fixture 已明确删除，只保留稳定的契约断言。
- **Type consistency:** `_parse_token` 全计划统一返回 `tuple[str, int] | None`；`login_response(username, token_version)` 统一两个参数；`PasswordChangeModal` 的 `userId` / `title` / `onClose` 在两个调用点一致；`sample_tags` 字段名在契约、前端与测试一致；`token_version` 在 db/migrate/auth/app 一致。
- **Scope:** 单个可信基线闭环，可在一次实施计划内完成；AI 点表导入另立规格。
