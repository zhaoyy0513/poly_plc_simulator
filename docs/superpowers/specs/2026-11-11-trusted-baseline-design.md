# 可信基线：登录收口 + 创建页专业化

日期：2026-11-11
状态：设计待用户复查

## 1. 问题与目标

当前工作树已有未提交的登录/用户管理实现，但推荐启动路径没有 `--init-admin`、README 仍写「没有登录鉴权」、测试环境变量可被外部覆盖、默认密码状态只展示不约束、会话在登出/改密后无服务端失效机制。与此同时，创建实例存在「平台擅自编造设备事实」的不专业口径：API 的 `sample_tags` 默认 `true`，而 UI 自建实例默认 `false`；link 切到 S7 / OPC UA 会自动勾选演示变量，生成温度/压力/计数器/运行状态及仿真值。

目标用户是工控联调工程师。成功标准：

- 全新 `data/` 按 README 任一种启动方式都能得到可登录管理员，且不会把明文口令回显给 API/UI。
- 登录用户可操作实例，管理员额外管理用户；两级权限行为有回归测试。
- UI 与 API 创建 sim / link 实例默认都是空地址空间或四个全零原始数据区，不默认生成演示变量。
- 创建页明确区分「连接必需参数」「演示模板」「空地址空间」，折叠高级参数，创建摘要说明客户端地址和变量数。
- 现有 sim/link 隔离、端口三段守卫、进程 detach/adopt、外部设备写入确认均不退化。

## 2. 推荐方向与取舍

先完成一个可信、可预测的小闭环：登录 WIP 收口 + 创建默认值与文案专业化。AI 点表导入作为独立一期；它需要稳定的登录、审计及人工确认交互，不能在本轮叠加 provider 配置、文件解析与提示词评测。

采用现有两级权限：登录用户可操作模拟器，管理员额外做用户管理。本轮不引入只读用户/操作员角色、不改所有写接口的授权矩阵。

不做：AI provider 配置、CSV/XLSX 导入、Agent/RAG、细粒度角色、OPC UA 节点浏览、`TagDef.initial_value`、静态预置值、协议引擎/运行时生命周期重构、docker driver、WS 推送、Postgres/Alembic。

## 3. 当前代码事实与设计边界

- `ServerCreate.sample_tags` 在 `packages/contracts/poly_contracts/models.py` 默认 `True`；`apps/control-plane/poly_control_plane/app.py::create_server` 在 `sample_tags` 为真时调用 `_default_tags()` 生成四个带 sine/random/ramp/toggle 的 `TagDef`。
- `apps/web/src/components/CreateServerModal.tsx` 的自建实例默认 `sampleTags=false`，但连接实例切到或选中 S7/OPC UA 时会设为 `true`，并把同一组演示变量写入 link 的标签表。
- Modbus 恒无标签，四个原始数据区默认 0–65535、`register_count` 默认 65536；`sim` 可由控制台预置 input/discrete，`link` 只读区不提供写入入口。
- `apps/control-plane/poly_control_plane/auth.py` 用 PBKDF2-HMAC-SHA256 存密码，用 `data/.secret` 的 Fernet 密钥签发 8 小时 Cookie token；`app.py` 的 HTTP 中间件保护除 `/api/login`、`/api/health` 外的 `/api/*`；`require_admin` 只保护用户列表、创建和删除。
- 未提交 WIP 已新增 `UserRow`、`audit.params`、登录页、用户管理弹窗、`tests/test_auth.py`，并把 `test_link.py` 的 HTTP fixture 改为登录后使用。现有全量验证为 81 pytest、前端 typecheck、31 条地址对照、vite build 全绿。

## 4. 功能设计

### 4.1 登录与会话收口

- `--init-admin NAME` 继续幂等：仅在用户表为空时创建管理员。README 的单端口与前后端分离启动命令都改为显式传入该参数，首次启动输出一次默认密码提醒；保留环境变量或 CLI 显式初始密码的可选入口，不把默认口令写进 API 响应。若实现选择「空库自动生成管理员」，必须同时保证无交互部署可登录且凭据只有启动日志可见；本设计选择显式 `--init-admin`，改动最小、行为可预测。
- README 的「限制 / 路线图」删除「没有登录鉴权、没有多用户」，补充 Cookie 会话、两级权限、默认口令必须修改、仅适合可信网络/HTTP 不加密传输的边界。
- 修复 `login` / `logout` 的 `Response` 返回注解：在 `app.py` 导入 `Response`，或移除注解让 FastAPI 直接返回 `Response`；不得保留悬空 `ForwardRef`。
- 修复 `tests/test_auth.py` 的环境变量顺序：临时 `POLY_DATA_DIR` 必须覆盖外部环境，不能反过来；HTTP 客户端继续 `trust_env=False`。
- `must_change_password` 不必做成强制改密向导，但必须命名/文案一致：首次登录由前端展示一次「当前仍是默认密码，请修改」提示，并在用户菜单提供「修改密码」入口（管理员与普通用户都可改自己）。用户管理弹窗仍保留管理员改他人密码。
- 密码修改要求新密码非空且有最小长度；后端 `PasswordChangeBody.password` 用 Pydantic 约束，前端输入同步校验。空密码/过短密码返回 422 或明确 400，并有测试覆盖。
- 已签发 Cookie 需要绑定密码版本：`UserRow` 增加 `token_version`（默认 0），token 载荷带 `ver`；改密、管理员改他人密码、删除用户后立即让旧 token 失效。中间件只解析 token 不查库；用户表校验由 `current_user` 完成，同时在中间件按用户名查询 `token_version` 做一次轻量校验，避免未访问 `current_user` 的路由仍接受旧凭据。`logout` 通过客户端删除 Cookie；不引入服务端 session 表。删除用户后旧 token 因用户不存在或版本不匹配被拒。
- 维持现有安全边界：`HttpOnly`、`SameSite=Strict`、8 小时过期；`secure=False` 仅因当前是内网明文 HTTP。保留 CORS `allow_credentials=False`；在规格与 README 明示这是可信 LAN 工具，不宣称具备 CSRF/传输层防护。新增一处简单 `Origin` 校验只在本轮若确有必要且不破坏 Vite 代理时启用，否则明确列为下一期。

### 4.2 两级权限

- 所有已登录用户可访问现有 site/server/tag/area/write/logs/audit 路由；只有 `GET/POST /api/users`、`DELETE /api/users/{id}` 要求管理员。
- `POST /api/users/{id}/password` 保持「本人或管理员」；管理员不能通过 API 提升其他用户为管理员（本轮没有该端点）。
- 普通用户菜单提供「修改密码」；管理员菜单额外提供「用户管理」。管理员不能删除自己；密码修改成功后当前用户的 `must_change_password` 清除，前端用户状态立即刷新。
- `api.audit()` 与 `AuditEntry` 保留为后续审计页入口；本轮不新增审计页面，不扩大审计保留策略。

### 4.3 创建默认值与文案

- 契约 `ServerCreate.sample_tags` 默认改为 `False`，与 UI 一致；`create_server` 仅在显式 `sample_tags=true` 时生成演示标签。
- 创建弹窗删除「切到 link S7/OPC UA 自动勾示例变量」逻辑；所有身份/协议的 `sampleTags` 初始为 `false`，切换协议或身份时保持 `false`。
- 「创建示例变量」改名为「加载演示模板」，文案明确：包含四个会随仿真变化的演示变量，仅用于冒烟测试，不是设备点表；对 link 实例默认隐藏，只有用户显式展开高级设置才能选择，且模板只生成平台侧标签定义、不向对端写值。
- 自建 S7/OPC UA 不勾选时创建空地址空间；文案从「所有值默认 0」改为「未定义变量，地址空间为空」。有标签时展示变量数；没有标签时不要暗示存在零值节点。
- `link` 的 S7/OPC UA 没有原始数据区，空标签表时创建后详情页给明确空状态与「去标签表添加对方真实地址 / NodeId」入口；不自动补演示点表。
- Modbus 保留 `register_count` 预填 `65536`，但标题改为「有效地址上限」并说明「四区共用、0–65535、留空等价」；输入框折叠在高级设置中。创建后摘要写「4 个原始数据区 · 每区 65536 点 · 未写入任何值」。
- OPC UA `endpoint_url` 留空时按现有 `OpcUaAdapter.endpoint_url()` 推导，并在创建摘要实时显示 `opc.tcp://<host>:<port>`；远程 site 用 `site.host`。该项仍可显式覆盖，折叠在高级设置。
- 协议必需参数收进可折叠「高级设置」：本机 `bind_ip`、OPC UA `endpoint_url`、Modbus `unit_id` / `register_count`、S7 `rack/slot`、OPC UA `namespace_index`、link `timeout_ms`。端口与目标地址保留在主区域。默认折叠，已有非空/非默认值或端口预检失败时自动展开。
- 创建页底部给单行「创建摘要」：身份、协议、客户端连接地址、变量/数据区数量、端口预检结论。摘要只陈述确定事实，不写「会自动生成值」。

### 4.4 数据流与边界

创建请求继续走 `POST /api/sites/{id}/servers`：后端按 `sample_tags` 决定是否生成标签，用 `validate_tags` 校验，再写 `ServerRow` / `TagRow` 并 `_launch`；`link` 仍先 `probe()` 成功才保存，`port=target_port`，跳过本机端口冲突与 bind 检查。演示模板不改变这一流程，仅提供明确传入的 `tags` / `sample_tags=true`。

`enabled` 仍是期望状态；停用后 `/area`、`/write` 返回 409，link 读写拒绝并由 `LinkSupervisor._require_active()` 兜底。控制面退出只 detach sim runtime，link 断开；这些约束不改。

## 5. 组件与影响面

- 契约：`packages/contracts/poly_contracts/models.py` 的 `ServerCreate.sample_tags` 默认值；`PasswordChangeBody` 不在共享包，留在 `auth.py`。
- 控制面：`auth.py`（token version、密码约束）、`db.py` / `migrate.py`（`user.token_version` 及旧库补列）、`app.py`（Response、token version 校验、用户菜单相关 API 行为、不改业务路由权限矩阵）、`__main__.py`（初始化管理员行为与日志）、README。
- 前端：`CreateServerModal.tsx`（默认值、高级设置、创建摘要、演示模板文案）、`App.tsx` / `UserManageModal.tsx` / `store.ts`（首次默认密码提示、用户菜单修改自己密码、状态刷新）、`styles.css`（可复用高级设置与摘要样式，遵循现有深浅色 token）、`api.ts` / `types.ts`（若新密码约束或 `token_version` 需前端传递则同步）。
- 测试：`tests/test_auth.py`（环境变量、密码约束、token 失效、两级权限、默认密码提示相关后端状态）、`tests/test_link.py`（保持登录后 HTTP fixture）、新增或扩展创建默认值回归，覆盖 API 省略 `sample_tags` 为 false、显式 true 仍生成演示、link 不自动示例、Modbus 无标签、端口检查不受折叠影响。

## 6. 错误处理

- 未登录、token 过期、token version 不匹配、用户已删除：统一 401，前端 `loadMe()` / `request()` 将用户置为未登录并跳登录页；轮询不再持续打 401。
- 新密码为空/过短：后端拒绝，保留旧密码与旧会话；前端显示具体错误。
- 管理员改他人密码成功后，该用户旧 Cookie 立即失效；当前管理员自己的会话不受影响，除非改的是自己。
- 创建页端口预检失败仍只是提示，保存时以数据库冲突 → 本机 bind → 远程 `ss -ltn` 三段检查为准；高级设置自动展开显示结论。
- link 探测失败不落库；设备应答但拒绝探测仍视作连接可用，不改现有 `LinkDeviceError` 分类。
- AI 外部服务错误不在本轮范围；不新增 provider 错误模型。

## 7. 验证

- `uv sync --group dev`（若依赖未变化可略）与 `uv run pytest`：覆盖全部 81 个现有测试及新增回归，全绿。重点核对 `test_auth.py`、`test_link.py` 和创建默认值。
- `npm --prefix apps/web run typecheck`、`npm --prefix apps/web run check:address`、`npm --prefix apps/web run build` 全绿。
- 全新临时 `POLY_DATA_DIR` 启动两种 README 命令，验证空库可登录、默认密码提示、改密后旧 token 失效、普通用户不能管用户但可查看实例。
- 浏览器走查：自建 OPC UA、link OPC UA、Modbus 创建页默认空/全零，高级设置可展开，创建摘要与最终客户端地址一致；深色/浅色主题都可读。
- 不动运行中的真实实例数据；测试一律使用临时数据目录和空闲端口。

## 8. 后续（明确不在本轮）

AI 点表导入另立规格：provider 配置与密钥加密、CSV/JSON 文件限制、严格目标 schema、现有 `validate_tags` 二次校验、原表到解析结果的人工确认页、外部发送二次确认、模糊字段弃权、审计与 fake-provider 回归。该规格依赖本轮稳定的登录、审计和用户确认交互，但不要求本轮预留抽象层。

## 9. 开放问题

无。默认口令提示采用前端一次性横幅，不阻断操作；两级权限、显式 `--init-admin`、演示模板默认关闭、高级设置默认折叠均已在本规格中定稿。
