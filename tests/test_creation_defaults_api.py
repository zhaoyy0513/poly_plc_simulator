"""Creation defaults, proven at the API rather than at the contract.

`test_creation_defaults.py` shows that `ServerCreate.sample_tags` defaults to
False. That is a statement about a Pydantic default; it says nothing about what
the control plane actually stores. These tests drive the real HTTP surface and
assert on the tag table that comes back, so a reverted default or a loosened
`_default_tags` gate fails here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from poly_contracts import Protocol
from poly_modbus import ModbusTcpAdapter

from .conftest import free_port
from .util import make_config


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    """The real control plane, as a real server, on a throwaway data dir.

    Same shape as the fixture in `test_link.py`: `app.py` binds its engine at
    import time, so a fresh interpreter with `POLY_DATA_DIR` pointed at a temp
    dir is the only way to exercise the HTTP surface without touching real data.
    """
    import httpx

    from poly_control_plane.system import ROOT

    data = tmp_path_factory.mktemp("poly-create-defaults")
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
            "admin1",
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
            resp = client.post(
                "/api/login", json={"username": "admin1", "password": "123456"}
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
    response = client.post("/api/sites", json={"name": "creation-defaults", "host": ""})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _tag_names(body: dict) -> list[str]:
    return [tag["name"] for tag in body["tags"]]


def test_s7_sim_omitting_sample_tags_creates_empty_tag_table(api) -> None:
    """No `sample_tags` in the request means no invented variables."""
    client = api
    site_id = _site(client)
    response = client.post(
        f"/api/sites/{site_id}/servers",
        json={
            "name": "s7-empty",
            "protocol": "s7",
            "bind_ip": "127.0.0.1",
            "port": free_port(),
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    server_id = body["id"]
    try:
        assert body["kind"] == "sim"
        assert body["tags"] == []
        # The stored table agrees with what creation reported.
        assert client.get(f"/api/servers/{server_id}").json()["tags"] == []
    finally:
        client.delete(f"/api/servers/{server_id}")


def test_s7_sim_explicit_sample_tags_seeds_demo_table(api) -> None:
    """An explicit `sample_tags=true` still loads the four demo variables."""
    client = api
    site_id = _site(client)
    response = client.post(
        f"/api/sites/{site_id}/servers",
        json={
            "name": "s7-demo",
            "protocol": "s7",
            "bind_ip": "127.0.0.1",
            "port": free_port(),
            "sample_tags": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    server_id = body["id"]
    try:
        assert _tag_names(body) == ["temperature", "pressure", "counter", "running"]
        assert _tag_names(client.get(f"/api/servers/{server_id}").json()) == [
            "temperature",
            "pressure",
            "counter",
            "running",
        ]
    finally:
        client.delete(f"/api/servers/{server_id}")


def test_modbus_sim_never_gets_tags(api) -> None:
    """Modbus has no tag table, so even an explicit request gets none."""
    client = api
    site_id = _site(client)
    response = client.post(
        f"/api/sites/{site_id}/servers",
        json={
            "name": "mb-no-tags",
            "protocol": "modbus_tcp",
            "bind_ip": "127.0.0.1",
            "port": free_port(),
            "sample_tags": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    server_id = body["id"]
    try:
        assert body["tags"] == []
        assert client.get(f"/api/servers/{server_id}").json()["tags"] == []
    finally:
        client.delete(f"/api/servers/{server_id}")


def test_modbus_link_omitting_sample_tags_creates_empty_tag_table(api) -> None:
    """A link is a client of somebody else's device: it never seeds a table."""
    client = api
    site_id = _site(client)
    config = make_config(Protocol.MODBUS_TCP, [], name="someone-elses-server")
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        response = client.post(
            f"/api/sites/{site_id}/servers",
            json={
                "name": "mb-link",
                "kind": "link",
                "protocol": "modbus_tcp",
                "port": config.port,
                "target_host": "127.0.0.1",
                "target_port": config.port,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        server_id = body["id"]
        assert body["kind"] == "link"
        assert body["tags"] == []
        assert client.get(f"/api/servers/{server_id}").json()["tags"] == []
        client.delete(f"/api/servers/{server_id}")
    finally:
        adapter.stop()
