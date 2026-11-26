"""AI 点表分析：把现场 Excel/CSV 变成一个或多个实例草稿。

只依赖 OpenAI 兼容的 `/chat/completions`。返回的内容一律当不可信输入：
先过 Pydantic，再做端口/标签/预置值校验。
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import openpyxl
from poly_contracts import Protocol, PresetItem, ServerCreate, ServerOptions, TagDef
from poly_core import validate_tags

from .db import AiConfigRow
from .secret import decrypt_secret, encrypt_secret
from .system import data_dir

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 200
BASE_PORT = {Protocol.MODBUS_TCP: 15020, Protocol.S7: 15102, Protocol.OPCUA: 15400}


class AiError(Exception):
    """面向用户的 AI 失败：消息会原样进 API detail。"""

    def __init__(self, detail: str, status_code: int = 502) -> None:
        super().__init__(detail)
        self.status_code = status_code


def parse_tabular(filename: str, data: bytes) -> list[dict[str, str]]:
    """把 .xlsx / .csv 读成行字典。只读第一个 sheet、第一行表头。"""
    if len(data) > MAX_FILE_BYTES:
        raise AiError(f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB 上限", 400)
    name = filename.lower()
    if name.endswith(".xlsx"):
        return _read_xlsx(data)
    if name.endswith(".csv"):
        return _read_csv(data)
    raise AiError("只支持 .xlsx 或 .csv 文件", 400)


def _clean_headers(rows: list[list[Any]]) -> list[dict[str, str]] | None:
    if not rows:
        return None
    headers = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
    out: list[dict[str, str]] = []
    for row in rows[1:]:
        if not any(cell not in (None, "") for cell in row):
            continue
        item = {
            headers[i]: ("" if i >= len(row) or row[i] is None else str(row[i]).strip())
            for i in range(len(headers))
            if headers[i]
        }
        out.append(item)
    return out


def _read_xlsx(data: bytes) -> list[dict[str, str]]:
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise AiError(f"Excel 解析失败：{exc}", 400) from exc
    try:
        sheet = workbook.worksheets[0]
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    parsed = _clean_headers(rows)
    if not parsed:
        raise AiError("Excel 里没有数据行", 400)
    return parsed


def _read_csv(data: bytes) -> list[dict[str, str]]:
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise AiError("CSV 编码无法识别（支持 UTF-8 / GBK）", 400)
    parsed = _clean_headers([row for row in csv.reader(io.StringIO(text))])
    if not parsed:
        raise AiError("CSV 里没有数据行", 400)
    return parsed


def build_prompt(
    protocol: Protocol, rows: list[dict[str, str]], description: str, used_ports: list[int]
) -> str:
    sample = json.dumps(rows[:MAX_ROWS], ensure_ascii=False)
    if len(rows) > MAX_ROWS:
        sample += f"\n（共 {len(rows)} 行，只取前 {MAX_ROWS} 行）"
    rules = {
        Protocol.MODBUS_TCP: (
            "Modbus 实例没有标签表。请输出 preset（原始数据区预置值）："
            "area 用 holding/input/coil/discrete，address 为 0 起的偏移，value 为 0–65535"
            "（coil/discrete 只能 0/1）。引用地址 40001 与 400001 都表示偏移 0。"
        ),
        Protocol.S7: (
            "S7 实例输出 tags：area 用 db/m/i/q，address 为字节偏移，"
            "db_number 为 DB 号（db 区必填），data_type 用 s7 支持的类型。"
        ),
        Protocol.OPCUA: (
            "OPC UA 实例输出 tags：name 必填，node_id 可留空"
            "（留空则按 ns=<namespace_index>;s=<name> 推导），address 可留空。"
        ),
    }[protocol]
    return f"""你是 PLC 点表解析助手。用户会上传现场点表（JSON 行）并给出描述。
请输出 JSON：{{"items": [实例草稿, ...], "warnings": ["被忽略或降级的行"]}}。

协议：{protocol.value}
{rules}
每个 item 必须含 name、port；可含 options、tags、preset。
port 必须是 1–65535 的整数，且互不相同；本机已占用端口：{used_ports or "无"}。
建议从 {BASE_PORT[protocol]} 起递增。
用户描述：{description or "（无）"}

点表行：
{sample}"""


def chat(
    cfg: Any,
    messages: list[dict[str, str]],
    max_tokens: int = 4096,
    api_key: str | None = None,
) -> str:
    """调 OpenAI 兼容端点，返回 message.content 文本。"""
    key = api_key or decrypt_secret(Path(data_dir()), cfg.api_key_enc)
    if not key:
        raise AiError("AI API key 未配置或已失效")
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    last = ""
    for _ in range(2):
        try:
            response = httpx.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=max(1.0, cfg.timeout_ms / 1000),
                trust_env=False,
            )
        except httpx.HTTPError as exc:
            last = str(exc)
            continue
        if response.status_code >= 400:
            raise AiError(f"AI 服务返回 {response.status_code}: {response.text[:500]}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            raise AiError(f"AI 响应格式无法解析：{exc}") from exc
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content
    raise AiError(f"AI 服务连接失败：{last}")


def list_models(cfg: Any, api_key: str | None = None) -> list[str]:
    """拉取 OpenAI 兼容 `/models` 的模型 id 列表。"""
    key = api_key or decrypt_secret(Path(data_dir()), cfg.api_key_enc)
    if not key:
        raise AiError("AI API key 未配置或已失效")
    url = cfg.base_url.rstrip("/") + "/models"
    last = ""
    for _ in range(2):
        try:
            response = httpx.get(
                url,
                headers={"Authorization": f"Bearer {key}"},
                timeout=max(1.0, cfg.timeout_ms / 1000),
                trust_env=False,
            )
        except httpx.HTTPError as exc:
            last = str(exc)
            continue
        if response.status_code >= 400:
            raise AiError(f"AI 服务返回 {response.status_code}: {response.text[:500]}")
        try:
            data = response.json().get("data", [])
        except Exception as exc:
            raise AiError(f"AI 模型列表无法解析：{exc}") from exc
        models = [
            str(item.get("id") or item.get("name"))
            for item in data
            if isinstance(item, dict) and (item.get("id") or item.get("name"))
        ]
        if not models:
            raise AiError("AI 服务没有返回可用模型")
        return models
    raise AiError(f"AI 服务连接失败：{last}")


def test_connection(cfg: Any, api_key: str | None = None) -> list[str]:
    """验证 base_url / key，并顺便带回模型列表。"""
    return list_models(cfg, api_key=api_key)


def parse_ai_json(content: str) -> tuple[list[dict[str, Any]], list[str]]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AiError(f"AI 返回的不是合法 JSON：{exc}", 400) from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise AiError("AI 返回缺少 items 列表", 400)
    warnings = [str(item) for item in data.get("warnings", []) if item]
    return data["items"], warnings


def _suggest_port(protocol: Protocol, used: set[int]) -> int:
    port = BASE_PORT[protocol]
    while port <= 65535 and port in used:
        port += 1
    if port > 65535:
        raise AiError("没有可用端口")
    used.add(port)
    return port


def analyze(
    cfg: Any,
    protocol: Protocol,
    rows: list[dict[str, str]],
    description: str,
    used_ports: list[int],
) -> dict[str, Any]:
    """把点表行 + 描述变成 ServerCreate 草稿。"""
    content = chat(cfg, [{"role": "user", "content": build_prompt(protocol, rows, description, used_ports)}])
    raw_items, warnings = parse_ai_json(content)
    used = set(used_ports)
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            warnings.append(f"第 {index + 1} 项不是对象，已忽略")
            continue
        name = str(raw.get("name") or f"ai-{protocol.value}-{index + 1}").strip()
        port_raw = raw.get("port")
        port: int | None = None
        if isinstance(port_raw, int) and 1 <= port_raw <= 65535 and port_raw not in used:
            port = port_raw
            used.add(port)
        else:
            port = _suggest_port(protocol, used)
            if port_raw is not None:
                warnings.append(f"第 {index + 1} 项端口 {port_raw!r} 不可用，已改为 {port}")
        if protocol is Protocol.MODBUS_TCP:
            if raw.get("tags"):
                warnings.append(f"第 {index + 1} 项是 Modbus，标签表已忽略")
            tags: list[TagDef] = []
        else:
            try:
                tags = [TagDef(**tag) for tag in raw.get("tags") or []]
            except Exception as exc:
                warnings.append(f"第 {index + 1} 项标签无法解析：{exc}")
                continue
        preset_raw = raw.get("preset") or []
        if preset_raw and protocol is not Protocol.MODBUS_TCP:
            warnings.append(f"第 {index + 1} 项不是 Modbus，预置值已忽略")
            preset_raw = []
        try:
            preset = [PresetItem(**item) for item in preset_raw]
            options = ServerOptions(**(raw.get("options") or {}))
            errors = validate_tags(protocol, tags)
            if errors:
                warnings.append(f"第 {index + 1} 项标签无效：{'; '.join(errors)}")
                continue
            draft = ServerCreate(
                name=name,
                kind="sim",
                source="ai",
                protocol=protocol,
                bind_ip=str(raw.get("bind_ip") or "0.0.0.0"),
                port=port,
                options=options,
                tags=tags,
                preset=preset,
            )
        except Exception as exc:
            warnings.append(f"第 {index + 1} 项无法使用：{exc}")
            continue
        items.append(draft.model_dump(mode="json"))
    return {"items": items, "warnings": warnings}


def save_config(
    session: Any,
    *,
    base_url: str,
    api_key: str,
    model: str,
    enabled: bool,
    timeout_ms: int,
) -> AiConfigRow:
    """保存单行 AI 配置。api_key 为空表示保留原 key。"""
    row = session.get(AiConfigRow, 1)
    if row is None:
        row = AiConfigRow(id=1)
        session.add(row)
    row.base_url = base_url.strip()
    if api_key:
        row.api_key_enc = encrypt_secret(Path(data_dir()), api_key)
    row.model = model.strip()
    row.enabled = enabled
    row.timeout_ms = timeout_ms
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    return row
