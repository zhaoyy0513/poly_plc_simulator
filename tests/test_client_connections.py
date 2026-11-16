"""Modbus client-connection tracking and kick."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
import pytest
from pymodbus.client import ModbusTcpClient

from poly_contracts import Protocol
from poly_control_plane.system import ROOT
from poly_core import ProtocolAdapter
from poly_modbus import ModbusTcpAdapter

from .conftest import free_port
from .util import make_config


def test_adapter_base_has_empty_connections_by_default() -> None:
    class Dummy(ProtocolAdapter):
        protocol = "dummy"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    adapter = Dummy(make_config(Protocol.MODBUS_TCP, []))
    assert adapter.connections() == []
    with pytest.raises(NotImplementedError):
        adapter.kick("nope")


def test_modbus_connections_are_tracked_and_can_be_kicked() -> None:
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        assert adapter.connections() == []

        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()
        # connect() only opens the socket; a real request is what the record counts.
        assert not client.read_holding_registers(0, count=1, device_id=1).isError()
        time.sleep(0.2)

        connections = adapter.connections()
        assert len(connections) == 1
        conn = connections[0]
        assert conn["remote"].startswith("127.0.0.1:")
        assert conn["requests"] >= 1
        assert conn["unit_id"] == 1
        assert "conn" not in conn
        conn_id = conn["id"]

        adapter.kick(conn_id)
        with pytest.raises(KeyError):
            adapter.kick(conn_id)
        time.sleep(0.3)
        assert adapter.connections() == []
        assert adapter.clients == 0

        client.close()
    finally:
        adapter.stop()


def test_runtime_control_lists_and_kicks_connections() -> None:
    from fastapi.testclient import TestClient

    from poly_sim_runtime.control import create_app
    from poly_sim_runtime.runner import RuntimeRunner

    config = make_config(Protocol.MODBUS_TCP, [])
    runner = RuntimeRunner(config)
    runner.start()
    time.sleep(0.2)
    try:
        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()
        time.sleep(0.2)
        with TestClient(create_app(runner)) as http:
            data = http.get("/connections").json()
            assert len(data["connections"]) == 1
            conn_id = data["connections"][0]["id"]

            response = http.post(f"/connections/{conn_id}/kick")
            assert response.status_code == 200
            assert response.json()["kicked"] == conn_id
            time.sleep(0.3)
            assert http.get("/connections").json()["connections"] == []

            assert http.post("/connections/nope/kick").status_code == 404
        client.close()
    finally:
        runner.stop()


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    data = tmp_path_factory.mktemp("poly-connections-api")
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
            assert http.post(
                "/api/login", json={"username": "admin1", "password": "123456"}
            ).status_code == 204
            yield http
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()


def _modbus_sim(api: httpx.Client) -> str:
    site = api.post("/api/sites", json={"name": "conn-site", "host": ""}).json()
    response = api.post(
        f"/api/sites/{site['id']}/servers",
        json={"name": "mb", "protocol": "modbus_tcp", "port": free_port()},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_connections_endpoint_is_modbus_only(api: httpx.Client) -> None:
    server_id = _modbus_sim(api)
    response = api.get(f"/api/servers/{server_id}/connections")
    assert response.status_code == 200
    assert response.json() == {"connections": []}

    site = api.post("/api/sites", json={"name": "s7-site", "host": ""}).json()
    s7 = api.post(
        f"/api/sites/{site['id']}/servers",
        json={"name": "s7", "protocol": "s7", "port": free_port()},
    )
    assert s7.status_code == 201, s7.text
    assert api.get(f"/api/servers/{s7.json()['id']}/connections").status_code == 400


def test_kick_unknown_connection_reports_gone(api: httpx.Client) -> None:
    server_id = _modbus_sim(api)
    response = api.post(f"/api/servers/{server_id}/connections/nope/kick")
    assert response.status_code == 400
