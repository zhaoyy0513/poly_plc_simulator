"""Reset clears the data plane without dropping clients."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
import pytest
from pymodbus.client import ModbusTcpClient

from poly_contracts import Protocol
from poly_modbus import ModbusTcpAdapter

from poly_control_plane.system import ROOT

from .conftest import free_port
from .util import make_config


def test_modbus_reset_zeroes_areas_and_keeps_clients() -> None:
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()
        assert client.write_register(5, 1234, device_id=1)
        assert client.write_coil(2, True, device_id=1)
        time.sleep(0.1)
        assert adapter.read_area("holding", 5, 1) == [1234]

        adapter.reset()

        assert adapter.read_area("holding", 5, 1) == [0]
        assert adapter.read_area("coil", 2, 1) == [False]
        # connection survived the reset
        assert adapter.clients == 1
        assert not client.read_holding_registers(5, count=1, device_id=1).isError()
        client.close()
    finally:
        adapter.stop()


def test_runtime_reset_endpoint_zeroes_data() -> None:
    from fastapi.testclient import TestClient

    from poly_sim_runtime.control import create_app
    from poly_sim_runtime.runner import RuntimeRunner

    config = make_config(Protocol.MODBUS_TCP, [])
    runner = RuntimeRunner(config)
    runner.start()
    time.sleep(0.2)
    try:
        runner.write_area("holding", 3, 77)
        with TestClient(create_app(runner)) as http:
            assert http.post("/reset").status_code == 200
        assert runner.read_area("holding", 3, 1) == [0]
    finally:
        runner.stop()


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    data = tmp_path_factory.mktemp("poly-maintenance-api")
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
        process.wait(timeout=10)
        log.close()


def _sim(api: httpx.Client, protocol: str = "modbus_tcp") -> str:
    site = api.post("/api/sites", json={"name": "m-site", "host": ""}).json()
    response = api.post(
        f"/api/sites/{site['id']}/servers",
        json={"name": "m", "protocol": protocol, "port": free_port()},
    )
    assert response.status_code == 201, response.text
    server_id = response.json()["id"]
    # Creation returns while the runtime is still "starting"; the data plane
    # answers only once the instance reports running.
    state: dict = {}
    for _ in range(150):
        state = api.get(f"/api/servers/{server_id}").json()
        if state["status"] != "starting":
            break
        time.sleep(0.1)
    assert state["status"] == "running", state
    return server_id


def test_reset_zeroes_and_audits(api: httpx.Client) -> None:
    server_id = _sim(api)
    try:
        assert api.post(
            f"/api/servers/{server_id}/area",
            json={"area": "holding", "address": 1, "value": 9},
        ).status_code == 200
        assert api.post(f"/api/servers/{server_id}/reset").status_code == 200
        area = api.get(
            f"/api/servers/{server_id}/area?area=holding&address=1&count=1"
        ).json()
        assert area["values"] == [0]
        audit = api.get("/api/audit").json()
        assert audit[0]["action"] == "server.reset"
    finally:
        api.delete(f"/api/servers/{server_id}")


def test_reset_rejects_link_and_disabled(api: httpx.Client) -> None:
    sim_id = _sim(api)
    try:
        sim = api.get(f"/api/servers/{sim_id}").json()
        link = api.post(
            f"/api/sites/{sim['site_id']}/servers",
            json={"name": "lk", "kind": "link", "protocol": "modbus_tcp",
                  "port": sim["port"], "target_host": "127.0.0.1",
                  "target_port": sim["port"]},
        )
        assert link.status_code == 201, link.text
        link_id = link.json()["id"]
        try:
            assert api.post(f"/api/servers/{link_id}/reset").status_code == 400
        finally:
            api.delete(f"/api/servers/{link_id}")

        assert api.patch(
            f"/api/servers/{sim_id}", json={"enabled": False}
        ).status_code == 200
        for _ in range(150):
            if api.get(f"/api/servers/{sim_id}").json()["status"] == "stopped":
                break
            time.sleep(0.1)
        assert api.post(f"/api/servers/{sim_id}/reset").status_code == 409
    finally:
        api.delete(f"/api/servers/{sim_id}")