"""Link instances: the platform as a client of somebody else's server.

Three layers, in order of how much they prove:

1. **Wire agreement.** Our client against our own server, over a real socket.
   If the two halves of the product disagree about what a frame means, nothing
   downstream matters.
2. **LinkSupervisor.** The status/event surface the UI reads, including the
   difference between "the device refused" and "the device is not there".
3. **HTTP end to end.** A link instance created through the real API, read and
   written through it, watched go away, and refused when it points nowhere.

The third layer runs the control plane in-process against a throwaway data
dir; see the `api` fixture for why the modules are re-imported.
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time

import pytest

from poly_contracts import Protocol
from poly_control_plane.link import LinkSupervisor, LinkTarget
from poly_modbus import ModbusError, ModbusException, ModbusTcpAdapter, ModbusTcpClient

from .util import free_port, make_config


# ------------------------------------------------------------ 1. wire agreement
def test_client_and_server_agree_on_all_four_areas() -> None:
    """A value written by the console must come back through the client, and
    the other way round - on every area, not just the friendly one."""
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        client = ModbusTcpClient("127.0.0.1", config.port, timeout=2.0)
        client.connect()

        client.write_area("holding", 100, 0x1234)
        assert client.read_area("holding", 100, 1) == [0x1234]
        client.write_area("coil", 7, True)
        assert client.read_area("coil", 7, 1) == [True]

        # Console side -> client side, on the read-only areas too.
        adapter.write_area("input", 8, 0xBEEF)
        adapter.write_area("discrete", 9, True)
        assert client.read_area("input", 8, 1) == [0xBEEF]
        assert client.read_area("discrete", 9, 1) == [True]

        # A multi-register write and a bit-block write, both FC16/FC15.
        client.write_registers(200, [1, 2, 3, 4])
        assert client.read_holding_registers(200, 4) == [1, 2, 3, 4]
        client.write_coils(300, [True, False, True])
        assert client.read_coils(300, 3) == [True, False, True]
        client.close()
    finally:
        adapter.stop()


def test_client_turns_a_device_refusal_into_an_exception() -> None:
    """"The device said no" is a different story from "the device is not there",
    and the UI has to be able to tell them apart."""
    port = free_port()
    ready = threading.Event()

    def serve() -> None:
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        ready.set()
        conn, _ = server.accept()
        try:
            header = conn.recv(7)
            tid = struct.unpack(">H", header[:2])[0]
            conn.recv(64)  # function + payload, small enough to arrive together
            # FC3 | 0x80 with exception code 0x02: illegal data address.
            conn.sendall(struct.pack(">HHHB", tid, 0, 3, 1) + bytes([0x80 | 3, 0x02]))
        finally:
            conn.close()
            server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)

    client = ModbusTcpClient("127.0.0.1", port, timeout=2.0)
    with pytest.raises(ModbusException) as excinfo:
        client.read_holding_registers(0, 1)
    assert excinfo.value.code == 0x02
    assert "地址越界" in str(excinfo.value)
    client.close()


def test_client_reports_an_unreachable_target_as_a_link_error() -> None:
    port = free_port()  # nothing is listening here
    client = ModbusTcpClient("127.0.0.1", port, timeout=0.5)
    with pytest.raises(ModbusError) as excinfo:
        client.read_holding_registers(0, 1)
    # The message is the *reason*, and it names the endpoint. Which reason it is
    # depends on the machine: Windows answers a dead port with a timeout, Linux
    # with a refusal. Both are "cannot reach it", and neither is a refusal by a
    # device, which is what ModbusException means.
    assert str(port) in str(excinfo.value)
    assert not isinstance(excinfo.value, ModbusException)


def test_client_reconnects_when_the_connection_is_lost() -> None:
    """Idle gateways drop sockets underneath a client that did nothing wrong.

    Two ways that happens, both of which must look like "reconnect and go
    through" rather than "the device said no": a socket the client closed
    itself, and a socket the far side killed while the client still held it.
    """
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    client = ModbusTcpClient("127.0.0.1", config.port, timeout=2.0)
    try:
        assert client.read_area("holding", 5, 1) == [0]

        # 1. Closed on our side: the next call has to establish a connection.
        client.close()
        client.write_area("holding", 5, 77)
        assert client.read_area("holding", 5, 1) == [77]

        # 2. Killed on theirs: the client still holds a socket that is gone.
        # Reaching for `_sock` is the point - a real gateway timeout leaves the
        # client in exactly this state, and there is no public way to fake it.
        assert client._sock is not None
        client._sock.close()
        client.write_area("holding", 6, 88)
        assert client.read_area("holding", 6, 1) == [88]
        client.close()
    finally:
        adapter.stop()


def test_client_refuses_to_write_read_only_areas() -> None:
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        client = ModbusTcpClient("127.0.0.1", config.port, timeout=2.0)
        for area, code in (("input", "04"), ("discrete", "02")):
            with pytest.raises(ModbusError) as excinfo:
                client.write_area(area, 0, 1)
            assert f"FC{code}" in str(excinfo.value)
        client.close()
    finally:
        adapter.stop()


# ------------------------------------------------------------ 2. LinkSupervisor
def _supervisor_on(port: int) -> LinkSupervisor:
    supervisor = LinkSupervisor(
        "link-test", ModbusTcpClient("127.0.0.1", port, timeout=2.0)
    )
    return supervisor


def test_supervisor_goes_running_and_keeps_history() -> None:
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        supervisor = _supervisor_on(config.port)
        state = supervisor.start(LinkTarget("127.0.0.1", config.port))
        assert state.status == "running"
        assert state.error is None
        assert state.latency_ms is not None and state.latency_ms >= 0
        assert state.endpoint == f"modbus-tcp://127.0.0.1:{config.port}"

        supervisor.write_area("holding", 42, 4321)
        assert supervisor.read_area("holding", 42, 1) == [4321]

        history = supervisor.logs()
        assert "连接正常" in history
        assert "写入 holding[42] = 4321" in history

        supervisor.stop()
        assert supervisor.state().status == "stopped"
        assert "已断开连接" in supervisor.logs()
    finally:
        adapter.stop()


def test_supervisor_reports_an_unreachable_target_as_an_error() -> None:
    port = free_port()
    supervisor = LinkSupervisor(
        "link-dead", ModbusTcpClient("127.0.0.1", port, timeout=0.5)
    )
    state = supervisor.start(LinkTarget("127.0.0.1", port, timeout=0.5))
    assert state.status == "error"
    assert str(port) in (state.error or "")
    # A second poll must not cost another timeout: it is remembered for a while.
    assert supervisor.state().status == "error"
    assert any("FAIL" in line for line in supervisor.logs().splitlines())


def test_supervisor_reads_and_writes_through_the_connection() -> None:
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        supervisor = _supervisor_on(config.port)
        supervisor.start(LinkTarget("127.0.0.1", config.port))
        # Whatever the platform writes must land in the server's data plane.
        supervisor.write_area("holding", 3, 999)
        assert adapter.read_area("holding", 3, 1) == [999]
        adapter.write_area("holding", 4, 888)
        assert supervisor.read_area("holding", 4, 1) == [888]

        with pytest.raises(ModbusError):
            supervisor.write_area("input", 4, 1)
        # No tag table on a link: the name is meaningless without an address.
        with pytest.raises(ModbusError):
            supervisor.write("temperature", 1.0)
    finally:
        adapter.stop()


# ------------------------------------------------------------ 3. HTTP end to end
@pytest.fixture(scope="module")
def api(tmp_path_factory):
    """The real control plane, as a real server, on a throwaway data dir.

    It has to be a separate process, not a thread: `app.py` binds its engine at
    *import* time, so by the time this module runs, something else has already
    imported the app against the developer's real `data/` directory. A fresh
    interpreter with `POLY_DATA_DIR` pointed at a temp dir is the only way to
    test the HTTP surface without touching (or trusting) real data.
    """
    import httpx
    import subprocess

    from poly_control_plane.system import ROOT

    data = tmp_path_factory.mktemp("poly-link-e2e")
    port = free_port()
    env = dict(os.environ, POLY_DATA_DIR=str(data))
    # To a file, not a pipe: a full pipe buffer would deadlock a chatty server,
    # and the log is the first thing worth reading when a request misbehaves.
    log_path = data / "server.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "poly_control_plane.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
        ],
        env=env,
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"

    def dump() -> str:
        log.flush()
        return log_path.read_text(encoding="utf-8", errors="replace")

    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"control plane exited:\n{dump()}")
            try:
                # trust_env=False: this machine has an ambient HTTP_PROXY, and a
                # proxied request to 127.0.0.1 comes back as an absolute-URI
                # mess that the server answers with a 404.
                if httpx.get(f"{base}/api/health", timeout=1.0, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError(f"control plane did not come up:\n{dump()}")
        with httpx.Client(base_url=base, timeout=15.0, trust_env=False) as client:
            yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - windows fallback
            process.kill()
            process.wait(timeout=10)
        log.close()


def _site(client) -> str:
    response = client.post("/api/sites", json={"name": "link-e2e", "host": ""})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_link_instance_is_created_read_and_written_over_http(api) -> None:
    client = api
    site_id = _site(client)

    # The "somebody else's server": a real Modbus TCP server, started outside
    # the platform, exactly like the machine on 18 环境 502.
    config = make_config(Protocol.MODBUS_TCP, [], name="someone-elses-server")
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    adapter.write_area("holding", 10, 0xABCD)
    try:
        response = client.post(
            f"/api/sites/{site_id}/servers",
            json={
                "name": "18 环境 502",
                "kind": "link",
                "protocol": "modbus_tcp",
                "port": 502,
                "target_host": "127.0.0.1",
                "target_port": config.port,
                "target_options": {"unit_id": 1, "timeout_ms": 2000},
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        server_id = body["id"]
        assert body["kind"] == "link"
        assert body["status"] == "running"
        assert body["error"] is None
        assert body["target_host"] == "127.0.0.1"
        assert body["target_port"] == config.port
        # A client has no listen address of its own; the port shown is the
        # target's, so the card and the connection never disagree.
        assert body["port"] == config.port
        assert body["latency_ms"] is not None

        # Read what the far side holds.
        response = client.get(
            f"/api/servers/{server_id}/area",
            params={"area": "holding", "address": 10, "count": 1},
        )
        assert response.status_code == 200, response.text
        assert response.json()["values"] == [0xABCD]
        assert response.json()["unit_id"] == 1

        # Write through the platform and see it land on the far side.
        response = client.post(
            f"/api/servers/{server_id}/area",
            json={"area": "holding", "address": 11, "value": 4242},
        )
        assert response.status_code == 200, response.text
        assert adapter.read_area("holding", 11, 1) == [4242]

        # The read-only areas are refused on the far side, with the reason.
        response = client.post(
            f"/api/servers/{server_id}/area",
            json={"area": "input", "address": 11, "value": 1},
        )
        assert response.status_code == 400
        assert "只读" in response.json()["detail"]

        # The log of a link is its connection history.
        response = client.get(f"/api/servers/{server_id}/logs")
        assert response.status_code == 200
        assert response.json()["link"] is True
        assert "连接正常" in response.json()["log"]

        # Disabling it is a real disconnect, not just a flag.
        response = client.patch(f"/api/servers/{server_id}", json={"enabled": False})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "stopped"

        client.delete(f"/api/servers/{server_id}")
    finally:
        adapter.stop()


def test_link_creation_is_refused_when_nothing_answers(api) -> None:
    client = api
    site_id = _site(client)
    port = free_port()
    response = client.post(
        f"/api/sites/{site_id}/servers",
        json={
            "name": "指向空处",
            "kind": "link",
            "protocol": "modbus_tcp",
            "port": 502,
            "target_host": "127.0.0.1",
            "target_port": port,
        },
    )
    assert response.status_code == 400
    assert "连不上" in response.json()["detail"]
    # Nothing was saved: a red card from birth helps nobody.
    listing = client.get(f"/api/sites/{site_id}/servers").json()
    assert listing == []


def test_link_update_repoints_only_to_a_live_target(api) -> None:
    client = api
    site_id = _site(client)
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        response = client.post(
            f"/api/sites/{site_id}/servers",
            json={
                "name": "可改指向",
                "kind": "link",
                "protocol": "modbus_tcp",
                "port": 502,
                "target_host": "127.0.0.1",
                "target_port": config.port,
            },
        )
        server_id = response.json()["id"]

        dead = free_port()
        response = client.patch(
            f"/api/servers/{server_id}", json={"target_port": dead}
        )
        assert response.status_code == 400
        assert "连不上" in response.json()["detail"]

        # The rejected edit must not have taken the working connection down.
        response = client.get(
            f"/api/servers/{server_id}/area",
            params={"area": "holding", "address": 0, "count": 1},
        )
        assert response.status_code == 200

        # A reachable new target is accepted, and the port follows it.
        other = make_config(Protocol.MODBUS_TCP, [])
        adapter2 = ModbusTcpAdapter(other)
        adapter2.start()
        time.sleep(0.2)
        try:
            adapter2.write_area("holding", 0, 5150)
            response = client.patch(
                f"/api/servers/{server_id}",
                json={"target_host": "127.0.0.1", "target_port": other.port},
            )
            assert response.status_code == 200, response.text
            assert response.json()["port"] == other.port
            assert response.json()["status"] == "running"
            response = client.get(
                f"/api/servers/{server_id}/area",
                params={"area": "holding", "address": 0, "count": 1},
            )
            assert response.json()["values"] == [5150]
        finally:
            adapter2.stop()
        client.delete(f"/api/servers/{server_id}")
    finally:
        adapter.stop()


def _wait_for(client, server_id: str, status: str, timeout: float = 10.0) -> dict:
    """A local instance binds its socket on a thread, so `start()` answers
    "starting" and the truth arrives a moment later - exactly as it does in the
    UI, which polls."""
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        body = client.get(f"/api/servers/{server_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.1)
    raise AssertionError(f"instance never became {status}: {body}")


def test_a_sim_instance_is_untouched_by_the_link_path(api) -> None:
    """The two kinds share a page but not a code path: a sim instance on a
    local site must still start, serve and stop exactly as before."""
    client = api
    site_id = _site(client)
    port = free_port()
    response = client.post(
        f"/api/sites/{site_id}/servers",
        json={
            "name": "本机自建",
            "protocol": "modbus_tcp",
            "bind_ip": "127.0.0.1",
            "port": port,
            "sample_tags": False,
        },
    )
    assert response.status_code == 201, response.text
    body = _wait_for(client, response.json()["id"], "running")
    assert body["kind"] == "sim"
    assert body["port"] == port
    assert "target_host" not in body

    # The console can preset a register on a sim instance...
    response = client.post(
        f"/api/servers/{body['id']}/area",
        json={"area": "input", "address": 2, "value": 1234},
    )
    assert response.status_code == 200, response.text
    response = client.get(
        f"/api/servers/{body['id']}/area",
        params={"area": "input", "address": 2, "count": 1},
    )
    assert response.json()["values"] == [1234]

    # ...including the areas a link may never write to.
    response = client.patch(f"/api/servers/{body['id']}", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    client.delete(f"/api/servers/{body['id']}")
