# 审计增强设计

日期：2026-11-13
状态：设计待用户复查

## 1. 问题与目标

当前审计表只保留 100 条，且很多写操作没记关键信息：`server.restart` 完全没记；`server.update` 只记了 `config_version`；`site.update` / `site.delete` / `server.delete` 的 params 为空；`area.write` 没记 area/address/value。前端只有 `api.audit()`，没有页面。

目标：
- 管理员能看到「谁在什么时候做了什么」，覆盖增删改和地址值写入；
- 保留足够长的历史（默认 1000 条，可配），避免刚删的东西过一会儿就查不到；
- 所有写操作带结构化 `params`，便于追溯；
- 为后续 AI 创建功能预留 `source: "ai"` 标记。

## 2. 范围

**做**：
- 审计保留策略 100 → 1000（`POLY_AUDIT_KEEP` 可配）；
- 补全所有写操作的审计点与结构化 params；
- `/api/audit` 支持过滤、分页、总数，权限收紧为管理员；
- 管理员可见的 `/audit` 页面（表格、过滤、自动刷新）。

**不做**（另立 spec 或后续）：
- AI 辅助创建（本设计只预留 `source`）；
- 审计导出 / 归档 / 长期存储；
- 普通用户查看审计（仅管理员）；
- 审计实时推送（用 5 秒轮询）。

## 3. 后端设计

### 3.1 保留策略

- `_audit()` 的 trim 上限从 100 改为 `POLY_AUDIT_KEEP`，默认 1000，最小 100。
- 读取方式：`int(os.environ.get("POLY_AUDIT_KEEP", "1000"))`，非法值回落到 1000。
- README / AGENTS.md 说明该环境变量。

### 3.2 审计点与 params

| action | 现状 | 目标 params / detail |
|---|---|---|
| `site.create` | 已有 name/host | 不变 |
| `site.update` | 空 | `params={"changed": {field: [old, new]}}`，只记录真实变更字段 |
| `site.delete` | 空 | `detail="name host"`，`params={"name", "host", "server_count"}` |
| `server.create` | 部分 | `params={"name", "protocol", "kind", "port", "bind_ip", "target", "tags_count", "source"}`，`source` 默认 `"manual"` |
| `server.update` | 只有 config_version | `params={"changed": {field: [old, new]}, "config_version"}`，覆盖 name/port/enabled/options/target 等真实变更 |
| `server.delete` | 空 | `detail="name protocol kind port"`，`params={"name", "protocol", "kind", "port"}` |
| `server.restart` | **缺失** | 新增 `params={"name"}` |
| `tags.replace` | 只有数量 | `params={"count", "names": [...前 50 个...]}` |
| `area.write` | 只有 detail 字符串 | `params={"area", "address", "value", "link": bool}` |
| `tag.write` | 已有 tag/value | 不变 |
| `user.*` / `login` / `logout` | 已有 | 不变 |

说明：
- `changed` 只记录**实际发生变化**的字段，值格式为 `[旧值, 新值]`；未变更的字段不出现。
- `server.update` 同时记录 `config_version`（新旧）与 `changed`。
- `source` 为 `"manual"`（页面创建）或 `"ai"`（AI 创建，后续 spec 使用）。

### 3.3 API

`GET /api/audit`：
- 权限：`require_admin`（从 `current_user` 收紧）。
- 查询参数：
  - `actor: str = ""`（精确匹配或包含，选包含）
  - `action: str = ""`（精确匹配，如 `server.create`；支持前缀 `server.` 过滤）
  - `q: str = ""`（对 `target` / `detail` 做不区分大小写的包含匹配）
  - `limit: int = 100`（1–500）
  - `offset: int = 0`（≥0）
- 返回：`{"items": [AuditRow...], "total": int}`，按 `id desc` 排序。
- `total` 是过滤后的总数，用于前端分页。

### 3.4 数据模型

- `AuditRow` 不变（id, created_at, actor, action, target, detail, params）。
- 无 schema 迁移。

## 4. 前端设计

### 4.1 路由与入口

- 新路由 `/audit`，页面组件 `apps/web/src/pages/AuditPage.tsx`。
- 左侧栏在「全部实例」下方加「审计」入口，仅 `user.is_admin` 可见；图标用已有的 `clock`。
- 页面标题「审计」，副标题说明用途。

### 4.2 表格

- 列：时间 / 操作人 / 动作 / 对象 / 详情 / 参数。
- **时间**：本地时间，精确到秒（`created_at` 是 UTC ISO，前端转本地）。
- **动作**：映射为中文标签（见下表），并保留原始 action 作为 title。
- **对象**：`target`。
- **详情**：`detail`。
- **参数**：默认显示格式化后的摘要（如 `port: 15020 → 15021`、`area: holding 0 = 1234`），可展开看完整 JSON。
- **`source=ai`** 的行在动作列加一个「AI」徽标。

动作中文映射（示例，实现时补全）：
`site.create` 新建 Site / `site.update` 修改 Site / `site.delete` 删除 Site /
`server.create` 新建实例 / `server.update` 修改实例 / `server.delete` 删除实例 / `server.restart` 重启实例 /
`tags.replace` 替换标签 / `area.write` 写数据区 / `tag.write` 写标签 /
`user.create` 新建用户 / `user.delete` 删除用户 / `user.password_change` 修改密码 /
`user.login` 登录 / `user.logout` 登出。

### 4.3 过滤器

- **操作人**：文本输入（包含匹配）。
- **动作**：下拉选择（全部 + 上述 action 列表）。
- **关键字**：文本输入，匹配 target/detail。
- 过滤参数变化时重置到第一页。
- **自动刷新**：5 秒轮询（复用 `usePolling`），页面隐藏时暂停。

### 4.4 分页与空状态

- 每页 50 条，底部「加载更多」按钮（offset 递增）。
- 空状态：「没有匹配的审计记录」。
- 加载失败：toast 提示。

## 5. 错误处理

- 非管理员访问 `/api/audit` → 403（中间件 + `require_admin` 双层）。
- 过滤参数非法（如负数 offset）→ 422（FastAPI 校验）。
- 审计写入与主操作在同一事务：审计失败则主操作失败，保证「要么都成功，要么都失败」。

## 6. 测试

- **后端**：
  - `/api/audit` 的过滤（actor/action/q）、分页（limit/offset）、总数；
  - 权限：普通用户 403，管理员 200；
  - 各审计点 params：`site.update` 的 `changed`、`server.restart` 存在、`area.write` 的 `{area, address, value, link}`、`server.delete` 的摘要、`tags.replace` 的 `names`；
  - 保留策略：写入超过 `POLY_AUDIT_KEEP` 后旧记录被清理。
- **前端**：`typecheck`、`build`；页面渲染走查（管理员可见、过滤、加载更多）。
- 现有 92 个测试保持绿。

## 7. 为 AI 预留

- `server.create` 的 params 增加 `source`（`"manual"` | `"ai"`）。
- AI spec 会在创建时传 `source: "ai"`，审计页对这类行显示「AI」徽标。

## 8. 开放问题

- 保留 1000 条是否足够？可通过 `POLY_AUDIT_KEEP` 调整；长期归档另立 spec。
- 是否需要审计导出（CSV）？后续。
