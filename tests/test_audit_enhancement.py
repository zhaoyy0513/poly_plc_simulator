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
from tests.util import kill_runtimes_under


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    data = tmp_path_factory.mktemp("poly-audit-api")
    port = free_port()
    env = {**os.environ, "POLY_DATA_DIR": str(data), "POLY_AUDIT_KEEP": "120"}
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


def test_audit_is_trimmed_to_configured_keep(api) -> None:
    # keep is clamped to a 100 floor, so a value above that is needed to observe
    # trimming; 120 is deliberate (the old hard-coded limit was 100).
    for i in range(130):
        assert api.post("/api/sites", json={"name": f"s{i}", "host": ""}).status_code == 201
    audit = api.get("/api/audit?limit=500").json()
    # /api/audit returns a list today and {items,total} once the audit API lands.
    total = audit["total"] if isinstance(audit, dict) else len(audit)
    assert total == 120


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
    audit = api.get("/api/audit?action=site.update").json()
    items = audit["items"] if isinstance(audit, dict) else audit
    assert items[0]["params"]["changed"] == {"name": ["a-site", "renamed"], "description": ["", "d"]}

    # server.create 带 source / tags_count
    server_id = _sim(api)
    audit = api.get("/api/audit?action=server.create").json()
    items = audit["items"] if isinstance(audit, dict) else audit
    assert items[0]["params"]["source"] == "manual"
    assert items[0]["params"]["tags_count"] == 0

    # server.update 记录真实变更
    api.patch(f"/api/servers/{server_id}", json={"name": "b", "port": free_port()})
    audit = api.get("/api/audit?action=server.update").json()
    items = audit["items"] if isinstance(audit, dict) else audit
    assert "name" in items[0]["params"]["changed"]
    assert "port" in items[0]["params"]["changed"]

    # restart 有审计
    api.post(f"/api/servers/{server_id}/restart")
    audit = api.get("/api/audit?action=server.restart").json()
    items = audit["items"] if isinstance(audit, dict) else audit
    assert len(items) >= 1

    # area.write 带 area/address/value/link
    api.post(f"/api/servers/{server_id}/area", json={"area": "holding", "address": 2, "value": 7})
    audit = api.get("/api/audit?action=area.write").json()
    items = audit["items"] if isinstance(audit, dict) else audit
    assert items[0]["params"] == {"area": "holding", "address": 2, "value": 7, "link": False}

    # server.delete 带摘要
    api.delete(f"/api/servers/{server_id}")
    audit = api.get("/api/audit?action=server.delete").json()
    items = audit["items"] if isinstance(audit, dict) else audit
    assert items[0]["params"]["name"] == "b"


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
