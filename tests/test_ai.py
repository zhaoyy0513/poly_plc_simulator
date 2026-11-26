"""AI 点表分析：文件解析、草稿校验、配置权限、预置值创建。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import httpx
import pytest
from openpyxl import Workbook

from poly_control_plane import ai as ai_mod
from poly_control_plane.system import ROOT
from poly_contracts import Protocol
from tests.conftest import free_port
from tests.util import kill_runtimes_under


@pytest.fixture()
def api(tmp_path):
    data = tmp_path / "poly-ai"
    data.mkdir()
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
        kill_runtimes_under(data)
        process.wait(timeout=10)
        log.close()


@pytest.fixture()
def fake_ai():
    """A minimal OpenAI-compatible server: records requests, answers with one draft."""
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            body = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            length = int(self.headers.get("Content-Length", 0))
            requests.append(json.loads(self.rfile.read(length)))
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "items": [
                                            {
                                                "name": "ai-1",
                                                "port": 15020,
                                                "preset": [{"area": "holding", "address": 0, "value": 9}],
                                            }
                                        ],
                                        "warnings": ["一行被忽略"],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _enable_ai(api, base_url: str) -> None:
    r = api.post(
        "/api/ai/config",
        json={
            "base_url": base_url,
            "api_key": "secret-key",
            "model": "test-model",
            "enabled": True,
            "timeout_ms": 60000,
        },
    )
    assert r.status_code == 200, r.text


def _site(api, name="ai-site"):
    return api.post("/api/sites", json={"name": name, "host": ""}).json()


# --------------------------------------------------------------- 文件解析

def test_parse_csv_utf8_and_gbk() -> None:
    rows = ai_mod.parse_tabular("a.csv", "名称,地址\nA,1\n".encode("utf-8-sig"))
    assert rows == [{"名称": "A", "地址": "1"}]
    rows = ai_mod.parse_tabular("b.csv", "名称,地址\nB,2\n".encode("gbk"))
    assert rows == [{"名称": "B", "地址": "2"}]
    with pytest.raises(ai_mod.AiError):
        ai_mod.parse_tabular("c.txt", b"x")


def test_parse_xlsx_first_sheet() -> None:
    book = Workbook()
    sheet = book.active
    sheet.append(["名称", "地址"])
    sheet.append(["A", 1])
    buf = io.BytesIO()
    book.save(buf)
    rows = ai_mod.parse_tabular("a.xlsx", buf.getvalue())
    assert rows == [{"名称": "A", "地址": "1"}]


# --------------------------------------------------------------- 草稿校验

def test_analyze_bumps_duplicate_port_and_keeps_preset(monkeypatch) -> None:
    cfg = SimpleNamespace(base_url="https://example.com", api_key_enc="", model="m", timeout_ms=1000)

    def fake_chat(_cfg, messages, max_tokens=4096, api_key=None):
        return json.dumps(
            {
                "items": [
                    {"name": "a", "port": 15020, "preset": [{"area": "holding", "address": 0, "value": 7}]},
                    {"name": "b", "port": 15020, "preset": []},
                ],
                "warnings": ["原始警告"],
            }
        )

    monkeypatch.setattr(ai_mod, "chat", fake_chat)
    result = ai_mod.analyze(cfg, Protocol.MODBUS_TCP, [{"地址": "40001"}], "描述", [])
    assert result["warnings"][0] == "原始警告"
    first, second = result["items"]
    assert first["preset"][0]["value"] == 7
    assert first["port"] == 15020
    assert second["port"] == 15021


def test_analyze_rejects_bad_json(monkeypatch) -> None:
    cfg = SimpleNamespace(base_url="https://example.com", api_key_enc="", model="m", timeout_ms=1000)
    monkeypatch.setattr(ai_mod, "chat", lambda *a, **k: "not json")
    with pytest.raises(ai_mod.AiError) as exc:
        ai_mod.analyze(cfg, Protocol.MODBUS_TCP, [], "", [])
    assert exc.value.status_code == 400


# --------------------------------------------------------------- 配置 API

def test_ai_config_never_returns_key(api, fake_ai) -> None:
    r = api.get("/api/ai/config")
    assert r.status_code == 200
    assert r.json()["configured"] is False

    base_url, _ = fake_ai
    _enable_ai(api, base_url)
    r = api.get("/api/ai/config")
    assert r.json()["configured"] is True
    assert r.json()["base_url"] == base_url
    assert "api_key" not in r.json()
    assert "api_key_enc" not in r.json()


def test_ai_models_endpoint(api, fake_ai) -> None:
    base_url, _ = fake_ai
    _enable_ai(api, base_url)
    r = api.get('/api/ai/models')
    assert r.status_code == 200
    assert r.json()['models'] == ['model-a', 'model-b']


def test_ai_config_requires_admin(api) -> None:
    assert api.post("/api/users", json={"username": "u1", "password": "pass123", "is_admin": False}).status_code == 201
    with httpx.Client(base_url=str(api.base_url), timeout=15, trust_env=False) as client:
        assert client.post("/api/login", json={"username": "u1", "password": "123456"}).status_code == 204
        assert client.get("/api/ai/config").status_code == 403
    assert httpx.get(f"{api.base_url}/api/ai/config", trust_env=False).status_code == 401


# --------------------------------------------------------------- 分析 API

def test_analyze_requires_enabled_config(api) -> None:
    site = _site(api)
    r = api.post(
        "/api/ai/analyze",
        data={"site_id": site["id"], "protocol": "modbus_tcp", "description": ""},
        files={"file": ("a.csv", b"name,addr\nA,1\n", "text/csv")},
    )
    assert r.status_code == 400


def test_analyze_returns_drafts_and_audit(api, fake_ai) -> None:
    base_url, requests = fake_ai
    _enable_ai(api, base_url)
    site = _site(api)
    r = api.post(
        "/api/ai/analyze",
        data={"site_id": site["id"], "protocol": "modbus_tcp", "description": "d"},
        files={"file": ("a.csv", b"name,addr\nA,1\n", "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["name"] == "ai-1"
    assert r.json()["warnings"] == ["一行被忽略"]
    prompt = requests[-1]["messages"][0]["content"]
    assert "A" in prompt and "已占用端口" in prompt
    audit = api.get("/api/audit?action=ai.analyze").json()
    assert audit["total"] == 1


# --------------------------------------------------------------- 预置值创建

def test_create_modbus_with_preset_writes_area(api) -> None:
    site = _site(api)
    r = api.post(
        f"/api/sites/{site['id']}/servers",
        json={
            "name": "preset",
            "protocol": "modbus_tcp",
            "port": free_port(),
            "options": {"register_count": 10},
            "preset": [{"area": "holding", "address": 1, "value": 42}],
        },
    )
    assert r.status_code == 201, r.text
    server = r.json()
    assert "warnings" not in server, server.get("warnings")
    area = api.get(f"/api/servers/{server['id']}/area?area=holding&address=0&count=5").json()
    assert area["values"][1] == 42


def test_create_modbus_preset_out_of_range_warns(api) -> None:
    site = _site(api)
    r = api.post(
        f"/api/sites/{site['id']}/servers",
        json={
            "name": "preset-oob",
            "protocol": "modbus_tcp",
            "port": free_port(),
            "options": {"register_count": 10},
            "preset": [{"area": "holding", "address": 20, "value": 1}],
        },
    )
    assert r.status_code == 201, r.text
    assert "超出有效范围" in r.json()["warnings"][0]


def test_create_s7_with_preset_rejected(api) -> None:
    site = _site(api)
    r = api.post(
        f"/api/sites/{site['id']}/servers",
        json={
            "name": "s7-preset",
            "protocol": "s7",
            "port": free_port(),
            "preset": [{"area": "holding", "address": 0, "value": 1}],
        },
    )
    assert r.status_code == 400
