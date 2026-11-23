# AI 点表分析设计（Modbus / S7 / OPC UA）

日期：2026-11-23
状态：设计待用户复查

## 1. 问题与目标

现场点表（Excel/CSV）表头不固定、地址写法不统一（Modbus 40001 与 400001、S7 DB/M/I/Q、OPC UA NodeId），手工录入慢且易错。目标：在「新建实例」旁提供「AI 分析」，上传文件 + 一句自然语言描述，AI 产出可预览、可编辑、可批量创建的实例草稿。

**本轮只做 OpenAI 兼容端点**（`base_url` + `api_key` + `model`），管理员在页面配置；Anthropic 原生端点预留 provider 抽象，不在本轮实现。

## 2. 范围

**做**：
- AI 连接配置（管理员）：base_url / api_key（加密）/ model / 启用开关 / 测试连接；
- 文件解析：`.xlsx`（openpyxl）与 `.csv`（stdlib），把行交给 AI；
- AI 分析：`site_id + protocol + 文件 + 自然语言描述` → N 个实例草稿（结构化 JSON）；
- 草稿预览与编辑：名称、端口、协议参数、标签/预置值；
- 批量创建：逐个走现有 `POST /api/sites/{site_id}/servers`，`source: "ai"`，失败可重试；
- Modbus 预置值：AI 可产出「地址 + 值」清单，创建后写入数据区（支持「同地址表、不同起始地址」的批量场景）。
- **本轮 AI 只产出 `kind=sim` 自建实例**；link 实例仍走原有创建流程。

**不做**：
- Anthropic 原生 SDK、多 provider 自动路由、Agent/工具循环；
- 审计导出、长期归档；
- 细粒度角色（配置仅管理员，使用仅需登录）；
- 自动命名端口（AI 可建议，后端/用户确认）。

## 3. 架构

```
前端 AI 分析弹窗
  → POST /api/ai/analyze (multipart: file, site_id, protocol, description)
      → 解析文件为行（openpyxl/csv）
      → OpenAI 兼容 /chat/completions（结构化 JSON，超时/重试）
      → 校验为 ServerCreate 草稿（Pydantic + validate_tags）
      → 返回 {items, warnings}
  → 用户预览/编辑 → 逐个 POST /api/sites/{site_id}/servers（source=ai）
```

- AI 配置存 DB 新表 `ai_config`（单行）：`base_url`、`api_key_enc`（Fernet，复用 `secret.py`）、`model`、`enabled`。
- AI 调用用 `httpx`（已是依赖），同步 POST，超时默认 60s，可配；失败重试 1 次。
- 结构化输出：请求 `response_format: {"type": "json_object"}`，响应 schema 由后端定义并强校验。
- 文件解析：`.xlsx` 读第一个 sheet 的表头与数据行；`.csv` 用 `csv.DictReader`（自动编码探测用 `utf-8-sig`，失败回落 `gbk`）。

## 4. 后端设计

### 4.1 AI 配置

- `ai_config` 表（SQLModel）：`id`（固定 1）、`base_url`、`api_key_enc`、`model`、`enabled`、`timeout_ms`、`updated_at`。新表由 `create_all()` 创建，无需迁移。
- `GET /api/ai/config`（admin）：返回 `{configured, base_url, model, enabled, timeout_ms}`，**不返回 key**。
- `POST /api/ai/config`（admin）：body `{base_url, api_key?, model, enabled, timeout_ms}`；`api_key` 为空表示保留原 key；保存前若 `enabled` 则测试连接。
- `POST /api/ai/test`（admin）：用当前配置发一次最小请求（如 `model` 列表或 1 token 对话），返回 `{ok, detail}`。

### 4.2 分析接口

`POST /api/ai/analyze`（登录用户，multipart）：
- 字段：`site_id`、`protocol`（modbus_tcp|s7|opcua）、`file`、`description`。
- 校验：site 存在；协议受支持；文件 ≤ 2MB；仅 `.xlsx/.csv`。
- 流程：解析行 → 组装 prompt（含协议点表规范、Modbus 引用宽度、S7 面积、OPC UA NodeId 规则）→ 调 AI → 解析 JSON → 逐条用 `ServerCreate` 校验（`source="ai"`）→ 返回 `{items, warnings}`。
- `items` 每条是 `ServerCreate` 形状（含 `name/protocol/kind/options/tags/preset`）；`warnings` 记录被丢弃/降级的行（缺列、无法解析地址、重复端口等）。
- 失败：配置缺失/未启用 → 400；AI 超时/5xx → 502 + 原文 detail；JSON 不合法 → 400 + 原因。

### 4.3 Modbus 预置值

- `ServerCreate` 增加 `preset: list[{area, address, value}] = []`（仅 Modbus 使用；S7/OPC UA 忽略）。
- `create_server` 创建实例后，若 `preset` 非空且协议为 Modbus，逐条 `supervisor.write_area(...)`（本地/远程同路径），失败的条目收集为警告，不影响实例创建（实例已建）。
- 预置值地址必须在 `register_count` 范围内，否则该条跳过并警告。

### 4.4 批量创建

- 前端逐个调用现有 `POST /api/sites/{site_id}/servers`，body 带 `source: "ai"`；后端复用现有端口/标签/link 探测逻辑。
- 端口：AI 可建议 `port`，但后端按现有规则校验；冲突时前端提示并允许编辑后重试。
- 审计：`server.create` 的 `params.source = "ai"`；批量创建额外记一条 `ai.batch`（params 含 item 数与成功/失败数）。

## 5. 前端设计

- `CreateServerModal` 旁加「AI 分析」按钮（登录用户可见；配置仅管理员）。
- 新组件 `AiAnalyzeModal.tsx`：
  - 选择协议（Modbus/S7/OPC UA）、上传 `.xlsx/.csv`、描述输入（例：「在此基础上创建另外 7 个，地址相同，起始地址分别 0/100/200…」）；
  - 「分析」→ 进度 → 预览表格（每条实例的名称/端口/标签数/预置值数），可编辑名称与端口；
  - 「创建 N 个实例」→ 逐个创建，显示成功/失败，失败可重试；
  - 未配置/未启用时提示「请管理员在 AI 配置中启用」。
- `AiConfigModal.tsx`（admin）：base_url/model/timeout/启用开关/测试连接/保存。
- `api.ts`：`aiAnalyze(formData)`、`aiConfig()`、`saveAiConfig(body)`、`testAiConfig()`。
- 审计页可加「AI」徽标（`params.source === 'ai'`）。

## 6. 安全

- API key 用 `data/.secret` 的 Fernet 加密存 `ai_config.api_key_enc`，API 永不返回明文；页面只显示「已配置」。
- AI 配置仅管理员；分析接口登录可用（创建实例本就需登录）。
- 上传文件仅用于解析，不执行宏/公式；限制 2MB；不落地到仓库目录（解析后即弃）。
- 发送给 AI 的内容可能含生产点表：分析前在前端明确提示，并在审计记录 `ai.analyze`（不含文件内容）。
- AI 返回一律当不可信输入：Pydantic 校验 + `validate_tags` + 端口/预置值范围校验。

## 7. 测试

- 后端：AI 配置保存/不回显 key/测试连接（fake HTTP）；analyze 的文件解析（xlsx/csv）、结构化输出校验、warnings、Modbus preset 写入、端口冲突、权限（非 admin 配置 403、非登录 401）；fake provider 回归。
- 前端：typecheck、build；手动走查上传→预览→批量创建→审计「AI」徽标。
- 全量 pytest 保持绿（当前 113）。

## 8. 开放问题 / 假设

- 假设「起始地址不一样」对 Modbus 而言体现为**预置值的地址偏移**；S7/OPC UA 体现为标签地址/NodeId 的不同。若用户指的是别的语义，需澄清。
- 假设批量创建允许前端逐个调用（复用现有 create 语义），不新增后端批量事务。
- 假设 Excel 只读第一个 sheet、第一行表头。
