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

from poly_contracts import Protocol, TagDef
from poly_control_plane.link import LinkSupervisor, LinkTarget
from poly_control_plane.link_clients import (
    LinkDeviceError,
    LinkStoppedError,
    build_link_client,
)
from poly_modbus import (
    ModbusError,
    ModbusException,
    ModbusReadOnlyError,
    ModbusTcpAdapter,
    ModbusTcpClient,
)
from poly_opcua import OpcUaAdapter, OpcUaClient
from poly_s7 import S7Adapter

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
            with pytest.raises(ModbusReadOnlyError) as excinfo:
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

        # A stopped link must not silently reconnect because somebody read a
        # data area. ModbusTcpClient reconnects on a closed socket by design,
        # so the supervisor has to refuse before the client sees the request.
        with pytest.raises(LinkStoppedError, match="已停用"):
            supervisor.read_area("holding", 42, 1)
        with pytest.raises(LinkStoppedError, match="已停用"):
            supervisor.write_area("holding", 42, 1)
        assert supervisor.state().status == "stopped"
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

        with pytest.raises(LinkDeviceError):
            supervisor.write_area("input", 4, 1)
        # No tag table on a link: the name is meaningless without an address.
        with pytest.raises(LinkDeviceError):
            supervisor.write("temperature", 1.0)
    finally:
        adapter.stop()


def test_a_refused_write_does_not_look_like_a_dead_link() -> None:
    """Regression: asking a real server to write a read-only area is a protocol
    refusal, not a connectivity failure. The device answered perfectly well, so
    the link must stay green - otherwise one refused write turns a working
    connection into a red card for the next five seconds."""
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        supervisor = _supervisor_on(config.port)
        supervisor.start(LinkTarget("127.0.0.1", config.port))
        assert supervisor.state().status == "running"

        with pytest.raises(LinkDeviceError):
            supervisor.write_area("discrete", 0, 1)

        assert supervisor.state().status == "running"
        assert supervisor.state().error is None
        # It is still in the history, because the user asked for it and should
        # be able to see what came back.
        assert any("只读" in line for line in supervisor.logs().splitlines())

        # And the connection still works afterwards.
        supervisor.write_area("holding", 1, 7)
        assert supervisor.read_area("holding", 1, 1) == [7]
        assert supervisor.state().status == "running"
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
            "poly_control_plane",
            "--init-admin",
            "testadmin",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
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
            # The auth middleware gates every /api route; log in as the admin
            # the fixture asked --init-admin to create. The client's cookie jar
            # then carries the session for every request in the test.
            resp = client.post(
                "/api/login", json={"username": "testadmin", "password": "123456"}
            )
            assert resp.status_code == 204, resp.text
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

        # Reading the data area through a stale page must not reconnect the
        # client. This was a real bug: the area endpoint had no enabled check
        # and ModbusTcpClient auto-reconnects on a closed socket.
        response = client.get(
            f"/api/servers/{server_id}/area",
            params={"area": "holding", "address": 0, "count": 1},
        )
        assert response.status_code == 409
        assert "已停用" in response.json()["detail"]
        response = client.post(
            f"/api/servers/{server_id}/area",
            json={"area": "holding", "address": 0, "value": 1},
        )
        assert response.status_code == 409
        response = client.get(f"/api/servers/{server_id}/values")
        assert response.status_code == 200
        assert response.json()["status"] == "stopped"
        response = client.get(f"/api/servers/{server_id}")
        assert response.json()["status"] == "stopped"

        client.delete(f"/api/servers/{server_id}")
    finally:
        adapter.stop()


def test_a_disabled_link_opens_no_new_connection(api) -> None:
    """Stopping a link must cost zero round trips, not one reconnecting one.

    `ModbusTcpClient` re-establishes its socket when it finds it closed, which
    is right for a live link and wrong for a deliberately disabled one: the
    API has to refuse before the client ever sees the request. The observable
    proof is on the far side - a device that sees no new TCP connection after
    the instance was disabled.

    This is the exact sequence a user hits: disable from the overview page,
    click through to the instance, press refresh on the data area.
    """
    client = api
    site_id = _site(client)
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    # `adapter.clients` is the *current* connection count: it drops to zero the
    # moment a client disconnects, so it cannot tell "never connected" from
    # "disconnected". Only a running total can prove a disabled link opened
    # nothing new, and the accept loop is the one place that knows.
    accepted: list[socket.socket] = []
    original_serve = adapter._serve

    def counting_serve(conn: socket.socket, conn_id: str) -> None:
        accepted.append(conn)
        original_serve(conn, conn_id)

    adapter._serve = counting_serve  # type: ignore[method-assign]
    try:
        response = client.post(
            f"/api/sites/{site_id}/servers",
            json={
                "name": "停用后不应重连",
                "kind": "link",
                "protocol": "modbus_tcp",
                "port": 502,
                "target_host": "127.0.0.1",
                "target_port": config.port,
            },
        )
        assert response.status_code == 201, response.text
        server_id = response.json()["id"]
        _wait_for(client, server_id, "running")
        before = len(accepted)
        assert before >= 1

        response = client.patch(f"/api/servers/{server_id}", json={"enabled": False})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "stopped"

        # Every read path a stale page can still fire, in the order the UI
        # fires them: values, then the data area, then a write.
        for _ in range(3):
            assert client.get(f"/api/servers/{server_id}/values").status_code == 200
            assert (
                client.get(
                    f"/api/servers/{server_id}/area",
                    params={"area": "holding", "address": 0, "count": 1},
                ).status_code
                == 409
            )
            assert (
                client.post(
                    f"/api/servers/{server_id}/area",
                    json={"area": "holding", "address": 0, "value": 1},
                ).status_code
                == 409
            )

        # Not one extra connection on the far side: the socket is gone and
        # nothing rebuilt it.
        assert len(accepted) == before

        # And the status stays stopped rather than being revived by a poll.
        assert client.get(f"/api/servers/{server_id}").json()["status"] == "stopped"
        client.delete(f"/api/servers/{server_id}")
    finally:
        adapter.stop()


def test_link_probe_treats_a_device_refusal_as_alive(api) -> None:
    """FC3 on address 0 is a probe, not a requirement.

    A real device can answer "illegal address" and still be perfectly usable.
    The test button must report that as reachable (with a warning), otherwise
    the user can never create a link to such a device.
    """
    client = api
    site_id = _site(client)
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
            conn.recv(64)
            conn.sendall(
                struct.pack(">HHHB", tid, 0, 3, 1) + bytes([0x80 | 3, 0x02])
            )
        finally:
            conn.close()
            server.close()

    threading.Thread(target=serve, daemon=True).start()
    assert ready.wait(2)

    response = client.post(
        f"/api/sites/{site_id}/link-test",
        params={"host": "127.0.0.1", "port": port},
        json={"unit_id": 1, "timeout_ms": 2000},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert "地址越界" in (body["warning"] or "")
    assert body["latency_ms"] is not None

    client.delete(f"/api/sites/{site_id}")


def test_link_rename_and_disable_do_not_need_a_live_target(api) -> None:
    """The edits a user reaches for when the far side is down must still work."""
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
                "name": "会掉线的连接",
                "kind": "link",
                "protocol": "modbus_tcp",
                "port": 502,
                "target_host": "127.0.0.1",
                "target_port": config.port,
                "target_options": {"unit_id": 1, "timeout_ms": 500},
            },
        )
        assert response.status_code == 201, response.text
        server_id = response.json()["id"]

        adapter.stop()
        time.sleep(0.2)

        # No probe on a name-only edit: this is how a broken link gets renamed.
        response = client.patch(
            f"/api/servers/{server_id}", json={"name": "离线也能改名"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "离线也能改名"

        # ...and how it gets switched off without waiting for a timeout.
        response = client.patch(f"/api/servers/{server_id}", json={"enabled": False})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "stopped"

        client.delete(f"/api/servers/{server_id}")
    finally:
        adapter.stop()


def test_all_servers_endpoint_reports_site_and_protocol_counts(api) -> None:
    """One list for the overview page, with the site each instance belongs to."""
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
                "name": "总览里的连接",
                "kind": "link",
                "protocol": "modbus_tcp",
                "port": 502,
                "target_host": "127.0.0.1",
                "target_port": config.port,
            },
        )
        assert response.status_code == 201, response.text
        server_id = response.json()["id"]

        response = client.get("/api/servers")
        assert response.status_code == 200, response.text
        row = next(item for item in response.json() if item["id"] == server_id)
        assert row["site_name"] == "link-e2e"
        assert row["site_local"] is True
        assert row["kind"] == "link"
        assert row["target_host"] == "127.0.0.1"
        assert row["target_port"] == config.port

        site = next(item for item in client.get("/api/sites").json() if item["id"] == site_id)
        assert site["protocol_counts"]["modbus_tcp"] == 1

        # A link does not occupy the target port on *this* machine.
        used = client.get("/api/system").json()["used_ports"]
        assert config.port not in used

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
    response = client.get(
        f"/api/servers/{body['id']}/area",
        params={"area": "holding", "address": 0, "count": 1},
    )
    assert response.status_code == 409

    # A disabled instance must not be started by the restart command: that
    # would silently override the desired state until the next reconcile.
    response = client.post(f"/api/servers/{body['id']}/restart")
    assert response.status_code == 409

    # Enabling is the supported way back, and it really binds the port again.
    response = client.patch(f"/api/servers/{body['id']}", json={"enabled": True})
    assert response.status_code == 200, response.text
    _wait_for(client, body["id"], "running")
    client.delete(f"/api/servers/{body['id']}")


def test_two_sims_of_the_same_protocol_are_isolated(api) -> None:
    """Two Modbus servers on one site are two processes with two data planes.

    Same protocol is not a conflict - the port is. This is the shape the user
    actually wants (2x Modbus plus 2x OPC UA on one machine), so it is proven
    at the wire rather than at the API: two instances that share a listener
    would both report "running" and only one would answer.
    """
    client = api
    site_id = _site(client)
    ports = [free_port(), free_port()]
    ids: list[str] = []
    for index, port in enumerate(ports):
        response = client.post(
            f"/api/sites/{site_id}/servers",
            json={
                "name": f"modbus-{index}",
                "protocol": "modbus_tcp",
                "bind_ip": "127.0.0.1",
                "port": port,
                "sample_tags": False,
            },
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
        _wait_for(client, ids[-1], "running")

    # Each console writes its own value ...
    for index, server_id in enumerate(ids):
        response = client.post(
            f"/api/servers/{server_id}/area",
            json={"area": "holding", "address": 0, "value": 1000 + index},
        )
        assert response.status_code == 200, response.text

    # ... and each *port* answers with its own instance's value. This is the
    # assertion a shared listener cannot pass.
    for index, port in enumerate(ports):
        probe = ModbusTcpClient("127.0.0.1", port, timeout=2.0)
        probe.connect()
        try:
            assert probe.read_holding_registers(0, 1) == [1000 + index]
        finally:
            probe.close()

    # Stopping one is invisible to the other.
    response = client.patch(f"/api/servers/{ids[0]}", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stopped"
    response = client.get(
        f"/api/servers/{ids[1]}/area", params={"area": "holding", "address": 0, "count": 1}
    )
    assert response.status_code == 200, response.text
    assert response.json()["values"] == [1001]

    # Deleting one is invisible too, and frees its port for a new instance of
    # the same protocol - which is the whole point of not counting instances.
    client.delete(f"/api/servers/{ids[0]}")
    assert client.get(f"/api/servers/{ids[0]}").status_code == 404
    response = client.get(
        f"/api/servers/{ids[1]}/area", params={"area": "holding", "address": 0, "count": 1}
    )
    assert response.json()["values"] == [1001]

    reclaimed = client.post(
        f"/api/sites/{site_id}/servers",
        json={
            "name": "modbus-again",
            "protocol": "modbus_tcp",
            "bind_ip": "127.0.0.1",
            "port": ports[0],
            "sample_tags": False,
        },
    )
    assert reclaimed.status_code == 201, reclaimed.text
    _wait_for(client, reclaimed.json()["id"], "running")

    for server_id in (ids[1], reclaimed.json()["id"]):
        client.delete(f"/api/servers/{server_id}")


def test_two_opcua_sims_on_one_site_are_isolated(api) -> None:
    """Two OPC UA servers: two endpoints, two address spaces.

    OPC UA binds the port named in its endpoint URL, so separate endpoints are
    also what keeps the two listeners apart.
    """
    client = api
    site_id = _site(client)
    ports = [free_port(), free_port()]
    ids: list[str] = []
    for index, port in enumerate(ports):
        response = client.post(
            f"/api/sites/{site_id}/servers",
            json={
                "name": f"opcua-{index}",
                "protocol": "opcua",
                "bind_ip": "127.0.0.1",
                "port": port,
                # No sim spec: a static value is the only way to prove the two
                # address spaces do not share memory.
                "tags": [
                    {"name": "counter", "data_type": "uint16", "area": "opcua", "address": 0}
                ],
            },
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
        body = _wait_for(client, ids[-1], "running")
        assert str(body["endpoint"]).endswith(f":{port}")

    for index, server_id in enumerate(ids):
        response = client.post(
            f"/api/servers/{server_id}/write", json={"tag": "counter", "value": 500 + index}
        )
        assert response.status_code == 200, response.text

    for index, server_id in enumerate(ids):
        values = client.get(f"/api/servers/{server_id}/values").json()["values"]
        assert values["counter"] == 500 + index

    # Stopping one leaves the other's address space untouched.
    client.patch(f"/api/servers/{ids[0]}", json={"enabled": False})
    values = client.get(f"/api/servers/{ids[1]}/values").json()["values"]
    assert values["counter"] == 501

    for server_id in ids:
        client.delete(f"/api/servers/{server_id}")


def test_a_limited_map_is_reported_and_enforced_over_http(api) -> None:
    """A configured map size is visible to the page and enforced on both paths.

    The UI needs the size to say "有效 0–99" and to refuse a range before
    asking; the wire needs it to answer ILLEGAL_ADDRESS like a real device.
    """
    client = api
    site_id = _site(client)
    port = free_port()
    response = client.post(
        f"/api/sites/{site_id}/servers",
        json={
            "name": "limited",
            "protocol": "modbus_tcp",
            "bind_ip": "127.0.0.1",
            "port": port,
            "options": {"register_count": 100},
        },
    )
    assert response.status_code == 201, response.text
    server_id = response.json()["id"]
    try:
        _wait_for(client, server_id, "running")

        inside = client.get(
            f"/api/servers/{server_id}/area",
            params={"area": "holding", "address": 0, "count": 100},
        )
        assert inside.status_code == 200, inside.text
        assert inside.json()["limit"] == 100

        outside = client.get(
            f"/api/servers/{server_id}/area",
            params={"area": "holding", "address": 90, "count": 20},
        )
        assert outside.status_code == 400
        assert "100 个地址" in outside.json()["detail"]

        refused = client.post(
            f"/api/servers/{server_id}/area",
            json={"area": "holding", "address": 100, "value": 1},
        )
        assert refused.status_code == 400
        assert "0–99" in refused.json()["detail"]

        # A real client must get the protocol answer, not a platform error.
        wire = ModbusTcpClient(host="127.0.0.1", port=port, timeout=3.0)
        wire.connect()
        try:
            with pytest.raises(ModbusException):
                wire.read_holding_registers(100, 1)
            assert wire.read_holding_registers(99, 1) == [0]
        finally:
            wire.close()
    finally:
        client.delete(f"/api/servers/{server_id}")


def test_port_check_answers_before_a_save_does(api) -> None:
    """The dialog asks the machine first: an occupied port is an answer, not
    an error, and it is the same verdict the save would give."""
    client = api
    site_id = _site(client)

    response = client.get(
        f"/api/sites/{site_id}/port-check",
        params={"port": free_port(), "bind_ip": "127.0.0.1"},
    )
    assert response.status_code == 200
    assert response.json()["available"] is True

    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    taken = squatter.getsockname()[1]
    try:
        response = client.get(
            f"/api/sites/{site_id}/port-check",
            params={"port": taken, "bind_ip": "127.0.0.1"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["available"] is False
        assert "已被占用" in body["reason"]
    finally:
        squatter.close()


def test_a_sim_port_held_by_another_process_is_refused(api) -> None:
    """The database is not the only claimant on a port. Whatever else on this
    machine already holds it has to be believed too, or the instance is born
    in error state with a row that insists it exists."""
    client = api
    site_id = _site(client)
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    taken = squatter.getsockname()[1]
    try:
        response = client.post(
            f"/api/sites/{site_id}/servers",
            json={
                "name": "抢不到端口",
                "protocol": "modbus_tcp",
                "bind_ip": "127.0.0.1",
                "port": taken,
                "sample_tags": False,
            },
        )
        assert response.status_code == 409
        assert "已被占用" in response.json()["detail"]
        # Nothing was saved: a row for an instance that can never start is
        # worse than no row at all.
        assert client.get(f"/api/sites/{site_id}/servers").json() == []
    finally:
        squatter.close()


def test_a_port_change_is_checked_but_a_rename_is_not(api) -> None:
    """The live bind test has one trap: the instance's own socket. A rename
    must stay clear of it, while moving to a taken port - including one held
    by something that is not one of ours - must still be refused."""
    client = api
    site_id = _site(client)
    port = free_port()
    response = client.post(
        f"/api/sites/{site_id}/servers",
        json={
            "name": "会搬家的实例",
            "protocol": "modbus_tcp",
            "bind_ip": "127.0.0.1",
            "port": port,
            "sample_tags": False,
        },
    )
    assert response.status_code == 201, response.text
    server_id = response.json()["id"]
    _wait_for(client, server_id, "running")

    # The cheap edit: no round trip to the machine, and certainly no refusal
    # from the very port the instance is holding.
    response = client.patch(f"/api/servers/{server_id}", json={"name": "改了名"})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "改了名"

    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    taken = squatter.getsockname()[1]
    try:
        response = client.patch(f"/api/servers/{server_id}", json={"port": taken})
        assert response.status_code == 409
        assert "已被占用" in response.json()["detail"]
        # The refused edit left the instance exactly where it was.
        response = client.get(f"/api/servers/{server_id}")
        assert response.json()["port"] == port
        assert response.json()["status"] == "running"
    finally:
        squatter.close()

    # A genuinely free port is accepted, and the instance really moves.
    moved = free_port()
    response = client.patch(f"/api/servers/{server_id}", json={"port": moved})
    assert response.status_code == 200, response.text
    _wait_for(client, server_id, "running")
    assert client.get(f"/api/servers/{server_id}").json()["port"] == moved

    client.delete(f"/api/servers/{server_id}")


def test_an_s7_link_is_created_read_and_written_over_http(api) -> None:
    """The same end-to-end story as the Modbus one, over the S7 wire: create,
    read the far side's real values through the link, write through it.

    The far side's tag table is read back from a temporary sim instance, so the
    two sides agree on addresses by construction instead of by a copy that can
    drift from `_default_tags`.
    """
    client = api
    site_id = _site(client)

    temp = client.post(
        f"/api/sites/{site_id}/servers",
        json={
            "name": "temp",
            "protocol": "s7",
            "bind_ip": "127.0.0.1",
            "port": free_port(),
            "sample_tags": True,
        },
    )
    assert temp.status_code == 201, temp.text
    seeded = [
        TagDef(**{key: value for key, value in item.items() if key in TagDef.model_fields})
        for item in temp.json()["tags"]
    ]
    client.delete(f"/api/servers/{temp.json()['id']}")

    far = S7Adapter(make_config(Protocol.S7, seeded, name="someone-elses-s7"))
    far.start()
    time.sleep(0.3)
    by_name = {tag.name: tag for tag in seeded}
    # Known values: a read that comes back as anything else proves nothing.
    far.write_tag(by_name["temperature"], 23.5)
    far.write_tag(by_name["counter"], 42)
    far.write_tag(by_name["running"], True)
    try:
        response = client.post(
            f"/api/sites/{site_id}/link-test",
            params={"host": "127.0.0.1", "port": far.config.port, "protocol": "s7"},
            json={"unit_id": 1, "rack": 0, "slot": 1, "timeout_ms": 2000},
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert response.json()["endpoint"].startswith("s7://")

        response = client.post(
            f"/api/sites/{site_id}/servers",
            json={
                "name": "S7 环境",
                "kind": "link",
                "protocol": "s7",
                "port": far.config.port,
                "target_host": "127.0.0.1",
                "target_port": far.config.port,
                "target_options": {"unit_id": 1, "rack": 0, "slot": 1, "timeout_ms": 2000},
                "sample_tags": True,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        server_id = body["id"]
        assert body["status"] == "running"
        assert body["target_options"]["rack"] == 0
        assert body["target_options"]["slot"] == 1
        assert [tag["name"] for tag in body["tags"]] == list(by_name)

        response = client.get(f"/api/servers/{server_id}/values")
        values = response.json()["values"]
        assert values["temperature"] == 23.5
        assert values["counter"] == 42
        # The M area is a different snap7 area than the DB one; a link that
        # only ever read DB blocks would report None here.
        assert values["running"] is True

        response = client.post(
            f"/api/servers/{server_id}/write", json={"tag": "counter", "value": 99}
        )
        assert response.status_code == 200, response.text
        assert far.read_tag(by_name["counter"]) == 99

        client.delete(f"/api/servers/{server_id}")
    finally:
        far.stop()


# ------------------------------------------------------------ 4. OPC UA link
def _seeded_tags(api, site_id: str, protocol: Protocol) -> list[TagDef]:
    """The protocol's own seed table, read back from a throwaway instance.

    Copying `_default_tags` into the test would let the two drift: the day a
    seed changes, the test would still pass while the product linked to the
    wrong addresses.
    """
    temp = api.post(
        f"/api/sites/{site_id}/servers",
        json={
            "name": "seed-source",
            "protocol": protocol.value,
            "bind_ip": "127.0.0.1",
            "port": free_port(),
            "sample_tags": True,
        },
    )
    assert temp.status_code == 201, temp.text
    seeded = [
        TagDef(**{key: value for key, value in item.items() if key in TagDef.model_fields})
        for item in temp.json()["tags"]
    ]
    api.delete(f"/api/servers/{temp.json()['id']}")
    return seeded


def test_opcua_client_reconnects_after_close() -> None:
    """`close()` must drop the session but not the machinery behind it.

    The link supervisor's whole lifecycle is built on this: disable calls
    `close()`, enable calls `probe()`. If closing also tore down the event loop
    thread, every re-enable would fail - and it would fail in the awkward way,
    with a working-looking client that never connects again.
    """
    config = make_config(Protocol.OPCUA, [TagDef(name="probe", data_type="float32")])
    adapter = OpcUaAdapter(config)
    adapter.start()
    try:
        client = OpcUaClient()
        client.configure("127.0.0.1", config.port, timeout=3.0)
        assert client.probe() >= 0
        assert client.connected is True

        client.close()
        assert client.connected is False

        # The point of the test: the same client object comes back.
        assert client.probe() >= 0
        assert client.connected is True
        client.dispose()
    finally:
        adapter.stop()


def test_a_link_probe_disposes_its_opcua_event_loop(monkeypatch, tmp_path) -> None:
    """A throwaway probe must not leave its event loop thread behind.

    `close()` is the *disable* half of an instance's lifecycle: it drops the
    session but deliberately keeps the OPC UA loop alive so the same client can
    be enabled again. A probe has no instance to come back to, so `close()`
    there leaks one `opcua-link` thread per click - measured: three clicks,
    three threads, forever. `dispose()` is the deletion half.

    The module is imported here, not at collection time, because `app.py` binds
    its database engine at import. Pointing `POLY_DATA_DIR` at a temp dir first
    keeps the developer's real `data/` out of it.
    """
    monkeypatch.setenv("POLY_DATA_DIR", str(tmp_path))
    from poly_control_plane.app import _probe_link_target

    def loop_threads() -> set[int]:
        return {t.ident for t in threading.enumerate() if t.name == "opcua-link"}

    before = loop_threads()
    for _ in range(3):
        # Nothing listens on this port: the probe fails, but only after it has
        # created the loop whose lifetime this test is about.
        result = _probe_link_target(
            LinkTarget(
                host="127.0.0.1",
                port=free_port(),
                protocol=Protocol.OPCUA,
                timeout=0.3,
            )
        )
        assert result["ok"] is False

    # dispose() asks the loop to stop; the thread needs a moment to wake up.
    deadline = time.time() + 3.0
    leaked = loop_threads() - before
    while leaked and time.time() < deadline:
        time.sleep(0.05)
        leaked = loop_threads() - before
    assert not leaked, f"三次探活泄漏了 {len(leaked)} 条 OPC UA 事件循环线程"


def test_an_opcua_link_is_created_read_and_written_over_http(api) -> None:
    """The same end-to-end story over the OPC UA wire.

    Two things are specific to this protocol and worth pinning down:

    * the namespace table is only visible after a real handshake, and the index
      a tag needs cannot be guessed - so the test button has to report it;
    * a tag with no `node_id` links to a simulator with nothing to configure,
      because our server names every variable `ns=<index>;s=<tag name>`.
    """
    client = api
    site_id = _site(client)
    seeded = _seeded_tags(client, site_id, Protocol.OPCUA)

    far = OpcUaAdapter(make_config(Protocol.OPCUA, seeded, name="someone-elses-opcua"))
    far.start()
    by_name = {tag.name: tag for tag in seeded}
    # Known values: a read that comes back as anything else proves nothing.
    far.write_tag(by_name["temperature"], 23.5)
    far.write_tag(by_name["counter"], 42)
    far.write_tag(by_name["running"], True)
    try:
        response = client.post(
            f"/api/sites/{site_id}/link-test",
            params={"host": "127.0.0.1", "port": far.config.port, "protocol": "opcua"},
            json={"timeout_ms": 3000},
        )
        assert response.status_code == 200, response.text
        probe = response.json()
        assert probe["ok"] is True
        assert probe["endpoint"].startswith("opc.tcp://")
        # The dialog shows this list so the user knows which index to type.
        assert "urn:poly:plc" in probe["namespaces"]

        response = client.post(
            f"/api/sites/{site_id}/servers",
            json={
                "name": "OPC UA 环境",
                "kind": "link",
                "protocol": "opcua",
                "port": far.config.port,
                "target_host": "127.0.0.1",
                "target_port": far.config.port,
                # namespace_index deliberately omitted: the default has to be
                # the one our own server uses, or linking to a simulator would
                # need manual configuration on every instance.
                "target_options": {"timeout_ms": 3000},
                "sample_tags": True,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        server_id = body["id"]
        assert body["status"] == "running"
        assert body["target_options"]["namespace_index"] == 2
        assert [tag["name"] for tag in body["tags"]] == list(by_name)

        values = client.get(f"/api/servers/{server_id}/values").json()["values"]
        assert values["temperature"] == 23.5
        assert values["counter"] == 42
        assert values["running"] is True

        # A write through the platform has to land on the far side.
        response = client.post(
            f"/api/servers/{server_id}/write", json={"tag": "counter", "value": 99}
        )
        assert response.status_code == 200, response.text
        assert far.read_tag(by_name["counter"]) == 99

        # No raw data area on an OPC UA link - the tag table is the whole view.
        response = client.get(
            f"/api/servers/{server_id}/area",
            params={"area": "holding", "address": 0, "count": 1},
        )
        assert response.status_code == 400
        assert "只支持 Modbus" in response.json()["detail"]

        # Disable, then enable again: the session was really dropped and the
        # same client object rebuilt it.
        response = client.patch(f"/api/servers/{server_id}", json={"enabled": False})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "stopped"
        assert client.get(f"/api/servers/{server_id}/values").json()["status"] == "stopped"

        response = client.patch(f"/api/servers/{server_id}", json={"enabled": True})
        assert response.status_code == 200, response.text
        _wait_for(client, server_id, "running")
        assert client.get(f"/api/servers/{server_id}/values").json()["values"]["counter"] == 99

        client.delete(f"/api/servers/{server_id}")
    finally:
        far.stop()


def test_an_opcua_bad_node_is_an_address_problem_not_an_outage() -> None:
    """`BadNodeIdUnknown` is an answer from a live server.

    A typo in a NodeId must stay in the event history and leave the card green.
    Turning it into a link failure would send the user hunting for a network
    problem that does not exist - and this is by far the likeliest mistake on
    OPC UA, because the address grammar is not obvious.
    """
    config = make_config(Protocol.OPCUA, [TagDef(name="real", data_type="float32")])
    adapter = OpcUaAdapter(config)
    adapter.start()
    try:
        typo = TagDef(name="typo", data_type="float32", node_id="ns=2;s=does-not-exist")
        supervisor = LinkSupervisor(
            "opcua-typo", build_link_client(Protocol.OPCUA), tags=[typo]
        )
        state = supervisor.start(
            LinkTarget(
                "127.0.0.1",
                config.port,
                protocol=Protocol.OPCUA,
                timeout=3.0,
                tags=[typo],
            )
        )
        assert state.status == "running"
        assert state.endpoint == f"opc.tcp://127.0.0.1:{config.port}"

        # A snapshot keeps the bad tag as None instead of failing the whole read.
        assert supervisor.values().values["typo"] is None

        with pytest.raises(LinkDeviceError):
            supervisor.write("typo", 1.0)

        # Still green, still usable, and the refusal is in the history.
        assert supervisor.state().status == "running"
        assert supervisor.state().error is None
        assert any("FAIL" in line for line in supervisor.logs().splitlines())

        # A disabled OPC UA link must refuse before the client can reconnect.
        supervisor.stop()
        assert supervisor.values().status == "stopped"
        with pytest.raises(LinkStoppedError):
            supervisor.write("typo", 1.0)
        supervisor.forget()
    finally:
        adapter.stop()
