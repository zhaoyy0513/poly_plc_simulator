# AI 点表分析实施计划

日期：2026-11-23
设计：`docs/superpowers/specs/2026-11-23-ai-point-table-design.md`

## 任务

### 1. 契约与后端 AI 模块

**契约**
- `packages/contracts/poly_contracts/models.py`：加 `PresetItem`（`area: str`、`address: int`、`value: int`，校验 coil/discrete 0/1、holding/input 0–65535），`ServerCreate.preset: list[PresetItem] = []`。

**DB**
- `apps/control-plane/poly_control_plane/db.py`：加 `AiConfigRow`（`id` 固定 1、`base_url`、`api_key_enc`、`model`、`enabled`、`timeout_ms=60000`、`updated_at`）。新表由 `create_all()` 创建，不进 `EXPECTED_COLUMNS`。

**AI 模块（新文件 `apps/control-plane/poly_control_plane/ai.py`）**
- `parse_tabular(filename, data: bytes) -> list[dict]`：`.xlsx` 用 openpyxl 读第一个 sheet（表头 + 行），`.csv` 用 `utf-8-sig` 失败回落 `gbk`；超过 2MB 抛错。
- `build_prompt(protocol, rows, description, used_ports) -> str`：含协议点表规范、Modbus 5/6 位引用、S7 面积、OPC UA NodeId、端口唯一性、最多 200 行。
- `chat(cfg, messages) -> str`：httpx POST `{base_url}/chat/completions`，Bearer key，`response_format=json_object`，超时 `timeout_ms`，失败重试 1 次；非 2xx 抛 `AiError(detail)`。
- `analyze(cfg, protocol, rows, description, used_ports) -> {items, warnings}`：解析 JSON（允许 ```json 包裹），逐条 `ServerCreate(source="ai", kind="sim", **item)` 校验；Modbus 必须有 `preset` 或空 tags，S7/OPC UA 必须有 tags；端口缺失/重复给 warning 并用建议端口补齐；无法解析的行进 warnings。
- key 加解密复用 `secret.encrypt_secret/decrypt_secret`。

**依赖**
- `apps/control-plane/pyproject.toml` 加 `openpyxl>=3.1`，`uv sync --group dev`。

**验证**：`uv run pytest tests/test_ai.py -q`（任务 1 的测试随任务 4 一起写，这里先跑通模块导入）。

### 2. 后端端点与创建链路

`apps/control-plane/poly_control_plane/app.py`：
- `GET /api/ai/config`（admin）→ `{configured, base_url, model, enabled, timeout_ms}`，不返回 key。
- `POST /api/ai/config`（admin）→ 保存；`api_key` 为空保留原 key；`enabled=true` 时先测试连接，失败 400。
- `POST /api/ai/test`（admin）→ 用当前配置发 1 token 请求，返回 `{ok, detail}`。
- `POST /api/ai/analyze`（登录）multipart：校验 site/协议/文件（≤2MB，.xlsx/.csv），读配置（未启用 400），解析行，调 `ai.analyze`，记 `ai.analyze` 审计（不含文件内容），返回 `{items, warnings}`。
- `create_server`：`kind=sim` 且协议为 Modbus 时，`_launch` 成功后逐条 `supervisor.write_area` 写 `preset`；越界/失败收集 warning 并在返回体加 `warnings`（仅非空时）。link 或 S7/OPC UA 传 preset → 400。
- 审计 `server.create` 已有 `source`，保持。

**验证**：`uv run pytest -q`（全量）。

### 3. 后端测试

`tests/test_ai.py`：
- 配置保存后 GET 不返回 key；非 admin 403；未登录 401。
- `parse_tabular`：xlsx（用 openpyxl 生成临时文件）与 csv（utf-8-sig / gbk）。
- `analyze`：fake provider（monkeypatch `ai.chat`）返回 2 个 Modbus 草稿 + 1 条 warning；端口重复时补齐；非法 JSON 抛错。
- `create_server` preset：Modbus sim 创建后数据区写入；越界条目跳过并 warning；S7 传 preset 400。
- `ai.analyze` 未启用 400；AI 502 时 502。

**验证**：`uv run pytest tests/test_ai.py -q`，再全量。

### 4. 前端

- `apps/web/src/types.ts`：`PresetItem`、`CreateServerBody.preset?`、`AiConfig`、`AiDraft`（ServerCreate + warnings）。
- `apps/web/src/api.ts`：`aiConfig()`、`saveAiConfig(body)`、`testAiConfig()`、`aiAnalyze(formData)`。
- `components/AiConfigModal.tsx`（admin）：base_url/model/timeout/启用/测试连接/保存；key 留空表示保留。
- `components/AiAnalyzeModal.tsx`：协议选择、文件上传、描述、「分析」→ 进度 → 预览（名称/端口可编辑，标签数/预置值数）→「创建 N 个实例」逐个 `createServer(source:'ai')`，显示成功/失败，失败可重试；未配置时提示管理员。
- `App.tsx`：用户菜单加「AI 配置」（admin），加 state/modal。
- `SitePage.tsx`：自建实例分组加「AI 分析」入口（登录可见），打开 `AiAnalyzeModal`。
- 审计页已有 AI 徽标，不改。

**验证**：`npm run typecheck`、`npm run build`、`npm run check:address`。

### 5. 文档与收尾

- `AGENTS.md`：AI 点表分析（配置、上传、批量创建、preset、审计）。
- `README.md`：使用入口与配置项。
- spec 状态改为已实现。
- 全量验证：`uv run pytest -q`、`npm run typecheck`、`npm run build`、`npm run check:address`、`git diff --check`。
- 最终代码复核。

## 不做

- link 实例的 AI 创建；Anthropic 原生端点；自动端口分配；多轮对话；文件长期存储。
