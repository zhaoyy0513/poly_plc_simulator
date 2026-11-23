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
