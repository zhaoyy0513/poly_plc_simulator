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
from .util import kill_runtimes_under, make_config


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


def test_s7_reset_zeroes_buffers_in_place() -> None:
    from poly_contracts import DataType, TagDef
    from poly_s7 import S7Adapter

    tags = [TagDef(name="temp", data_type=DataType.FLOAT32, area="db", db_number=1, address=0)]
    adapter = S7Adapter(make_config(Protocol.S7, tags))
    adapter.write_tag(tags[0], 42.5)
    adapter.reset()
    assert adapter._buffers[("db", 1)][:4] == bytes(4)


def test_opcua_reset_restores_seed_values(monkeypatch) -> None:
    from poly_contracts import DataType, TagDef
    from poly_opcua import OpcUaAdapter

    # SimSpec defaults to SimMode.STATIC, whose seed_value for a float is 0.
    tags = [TagDef(name="temp", data_type=DataType.FLOAT32, area="opcua", address=0,
                   writable=True)]
    adapter = OpcUaAdapter(make_config(Protocol.OPCUA, tags))
    written = []
    monkeypatch.setattr(adapter, "write_tag", lambda tag, value: written.append(value))
    adapter._nodes[tags[0].name] = object()  # pretend nodes exist
    adapter.reset()
    assert len(written) == 1 and written[0] == 0.0


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
        kill_runtimes_under(data)
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
        assert audit["items"][0]["action"] == "server.reset"
    finally:
        api.delete(f"/api/servers/{server_id}")


def test_maintenance_only_kills_test_orphans(tmp_path, monkeypatch) -> None:
    from poly_control_plane import maintenance

    rows = [
        {"pid": 111, "create_time": 1.0, "config": str(tmp_path / "pytest-of-x" / "a.json"), "control_port": 1, "owned": False, "is_test": True},
        {"pid": 222, "create_time": 2.0, "config": str(tmp_path / "real" / "b.json"), "control_port": 2, "owned": False, "is_test": False},
        {"pid": 333, "create_time": 3.0, "config": str(tmp_path / "pytest-of-x" / "c.json"), "control_port": 3, "owned": True, "is_test": True},
    ]
    monkeypatch.setattr(maintenance, "list_runtimes", lambda data_dir: rows)
    # owned and non-test must be refused
    with pytest.raises(ValueError):
        maintenance.kill_test_runtimes([222], tmp_path)
    with pytest.raises(ValueError):
        maintenance.kill_test_runtimes([333], tmp_path)
    with pytest.raises(ValueError):
        maintenance.kill_test_runtimes([111, 222], tmp_path)
    # a valid test orphan passes validation
    monkeypatch.setattr(maintenance, "psutil", _FakePsutil({111}))
    assert maintenance.kill_test_runtimes([111], tmp_path) == [111]


class _FakePsutil:
    def __init__(self, alive: set[int]) -> None:
        self._alive = alive

    def Process(self, pid: int):  # noqa: N802 (psutil naming)
        outer = self

        class Proc:
            def terminate(self) -> None:
                pass

            def wait(self, timeout: float | None = None) -> None:
                if pid not in outer._alive:
                    raise maintenance.psutil.NoSuchProcess(pid)

        return Proc()

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    class TimeoutExpired(Exception):
        pass


def test_maintenance_list_requires_admin(api: httpx.Client) -> None:
    assert api.post("/api/users", json={"username": "m-viewer"}).status_code == 201
    admin_cookie = api.cookies.get("poly_auth")
    try:
        api.cookies.clear()
        assert api.post(
            "/api/login", json={"username": "m-viewer", "password": "123456"}
        ).status_code == 204
        assert api.get("/api/maintenance/runtimes").status_code == 403
    finally:
        api.cookies.set("poly_auth", admin_cookie)
        api.delete("/api/users/" + _user_id(api, "m-viewer"))


def _user_id(api: httpx.Client, username: str) -> str:
    users = api.get("/api/users").json()
    return next(u["id"] for u in users if u["username"] == username)


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
