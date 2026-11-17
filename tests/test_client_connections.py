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
from .util import kill_runtimes_under, make_config


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
        # Windows terminate() is a hard kill: the control plane gets no shutdown
        # path, so sim runtime children it spawned survive as orphans.
        kill_runtimes_under(data)
        process.wait(timeout=10)
        log.close()


def _modbus_sim(api: httpx.Client) -> str:
    site = api.post("/api/sites", json={"name": "conn-site", "host": ""}).json()
    response = api.post(
        f"/api/sites/{site['id']}/servers",
        json={"name": "mb", "protocol": "modbus_tcp", "port": free_port()},
    )
    assert response.status_code == 201, response.text
    server_id = response.json()["id"]
    # Creation returns while the runtime is still "starting"; the connection
    # routes answer 400 until the instance reports running, so wait for it.
    state: dict = {}
    for _ in range(150):
        state = api.get(f"/api/servers/{server_id}").json()
        if state["status"] != "starting":
            break
        time.sleep(0.1)
    assert state["status"] == "running", state
    return server_id


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


def _wait_for_status(api: httpx.Client, server_id: str, status: str) -> dict:
    """Poll until the instance reports `status`; a PATCH returns the desired
    state before the runtime has finished acting on it."""
    deadline = time.time() + 15.0
    body: dict = {}
    while time.time() < deadline:
        body = api.get(f"/api/servers/{server_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.1)
    raise AssertionError(f"instance never became {status}: {body}")


def test_connection_routes_refuse_a_disabled_instance(api: httpx.Client) -> None:
    server_id = _modbus_sim(api)
    try:
        response = api.patch(f"/api/servers/{server_id}", json={"enabled": False})
        assert response.status_code == 200, response.text
        _wait_for_status(api, server_id, "stopped")

        assert api.get(f"/api/servers/{server_id}/connections").status_code == 409
        assert (
            api.post(f"/api/servers/{server_id}/connections/nope/kick").status_code
            == 409
        )
    finally:
        api.delete(f"/api/servers/{server_id}")


def test_connections_endpoint_refuses_a_link_instance(api: httpx.Client) -> None:
    sim_id = _modbus_sim(api)
    sim = api.get(f"/api/servers/{sim_id}").json()
    # Point the link at the self-built instance just created: no extra server
    # to stand up, and the probe on creation has a live target to reach.
    response = api.post(
        f"/api/sites/{sim['site_id']}/servers",
        json={
            "name": "mb-link",
            "kind": "link",
            "protocol": "modbus_tcp",
            "port": sim["port"],
            "target_host": "127.0.0.1",
            "target_port": sim["port"],
        },
    )
    assert response.status_code == 201, response.text
    link_id = response.json()["id"]
    try:
        assert api.get(f"/api/servers/{link_id}/connections").status_code == 400
    finally:
        api.delete(f"/api/servers/{link_id}")
        api.delete(f"/api/servers/{sim_id}")


def test_connections_endpoint_refuses_an_opcua_instance(api: httpx.Client) -> None:
    site = api.post("/api/sites", json={"name": "ua-conn-site", "host": ""}).json()
    response = api.post(
        f"/api/sites/{site['id']}/servers",
        json={"name": "ua", "protocol": "opcua", "port": free_port()},
    )
    assert response.status_code == 201, response.text
    server_id = response.json()["id"]
    try:
        assert api.get(f"/api/servers/{server_id}/connections").status_code == 400
    finally:
        api.delete(f"/api/servers/{server_id}")


def test_kick_writes_an_audit_record(api: httpx.Client) -> None:
    server_id = _modbus_sim(api)
    port = api.get(f"/api/servers/{server_id}").json()["port"]
    client = ModbusTcpClient("127.0.0.1", port=port)
    assert client.connect()
    try:
        assert not client.read_holding_registers(0, count=1, device_id=1).isError()
        time.sleep(0.2)
        connections = api.get(
            f"/api/servers/{server_id}/connections"
        ).json()["connections"]
        assert len(connections) == 1
        conn = connections[0]

        response = api.post(
            f"/api/servers/{server_id}/connections/{conn['id']}/kick"
        )
        assert response.status_code == 200, response.text
        assert response.json()["kicked"] == conn["id"]

        rows = api.get("/api/audit").json()
        latest = rows[0]
        assert latest["action"] == "server.client_kick"
        assert latest["actor"] == "admin1"
        assert latest["params"]["conn_id"] == conn["id"]
        assert latest["params"]["remote"] == conn["remote"]
    finally:
        client.close()
        api.delete(f"/api/servers/{server_id}")


def test_connection_routes_map_supervisor_errors_to_400(monkeypatch) -> None:
    """A remote supervisor that cannot reach the runtime is a 400, not a 500."""
    import importlib
    import types

    from fastapi import HTTPException
    from sqlmodel import Session, SQLModel, create_engine

    from poly_control_plane.db import ServerRow, SiteRow
    from poly_control_plane.deploy import SshError

    # `poly_control_plane.app` is shadowed by the FastAPI instance re-exported in
    # the package __init__, so fetch the module from sys.modules explicitly.
    app_module = importlib.import_module("poly_control_plane.app")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        site = SiteRow(id="site-conn-err", name="conn-err")
        server = ServerRow(
            id="srv-conn-err", site_id=site.id, name="mb", protocol="modbus_tcp", port=15020
        )
        session.add_all([site, server])
        session.commit()

        class ListBoom:
            def connections(self, server_id: str):
                raise SshError("实例未在运行")

            def kick_connection(self, server_id: str, conn_id: str) -> None:
                raise SshError("实例未在运行")

        class KickBoom:
            def connections(self, server_id: str):
                return []

            def kick_connection(self, server_id: str, conn_id: str) -> None:
                raise SshError("实例未在运行")

        request = types.SimpleNamespace(app=object())
        monkeypatch.setattr(
            app_module, "_runtime", lambda app, session, site_id: (ListBoom(), None)
        )
        with pytest.raises(HTTPException) as listed:
            app_module.server_connections(server.id, request, session)
        assert listed.value.status_code == 400

        monkeypatch.setattr(
            app_module, "_runtime", lambda app, session, site_id: (KickBoom(), None)
        )
        with pytest.raises(HTTPException) as kicked:
            app_module.kick_server_connection(server.id, "nope", request, session)
        assert kicked.value.status_code == 400
