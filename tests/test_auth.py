"""Auth, user management, and audit logging tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
import pytest

from tests.conftest import free_port


@pytest.fixture(scope="module")
def auth_server(tmp_path_factory):
    """Fresh control plane with auth enabled, on a throwaway data dir."""
    from poly_control_plane.system import ROOT

    data = tmp_path_factory.mktemp("poly-auth")
    port = free_port()
    env = {**os.environ, "POLY_DATA_DIR": str(data)}
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
                if httpx.get(
                    f"{base}/api/health", timeout=1.0, trust_env=False
                ).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError(f"control plane did not come up:\n{dump()}")
        with httpx.Client(base_url=base, timeout=10.0, trust_env=False) as client:
            yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_unauthenticated_request_returns_401(auth_server: httpx.Client) -> None:
    response = auth_server.get("/api/sites")
    assert response.status_code == 401
    assert "未登录" in response.json()["detail"]


def test_login_and_me(auth_server: httpx.Client) -> None:
    response = auth_server.post(
        "/api/login", json={"username": "admin1", "password": "123456"}
    )
    assert response.status_code == 204
    assert "poly_auth" in response.headers.get("set-cookie", "")

    me = auth_server.get("/api/me").json()
    assert me["username"] == "admin1"
    assert me["is_admin"] is True
    assert me["must_change_password"] is True


def test_wrong_password_rejected(auth_server: httpx.Client) -> None:
    response = auth_server.post(
        "/api/login", json={"username": "admin1", "password": "wrong"}
    )
    assert response.status_code == 401
    assert "用户名或密码错误" in response.json()["detail"]


def test_user_management_requires_admin(auth_server: httpx.Client) -> None:
    # Log in as admin, create a regular user
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})
    auth_server.post("/api/users", json={"username": "bob"})

    # Log in as bob
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "bob", "password": "123456"})

    # bob cannot list users
    response = auth_server.get("/api/users")
    assert response.status_code == 403

    # bob cannot create users
    response = auth_server.post("/api/users", json={"username": "charlie"})
    assert response.status_code == 403


def test_admin_can_create_and_delete_users(auth_server: httpx.Client) -> None:
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})

    # Create
    response = auth_server.post("/api/users", json={"username": "alice"})
    assert response.status_code == 201
    alice = response.json()
    assert alice["username"] == "alice"
    assert alice["is_admin"] is False
    assert alice["must_change_password"] is True

    # List
    users = auth_server.get("/api/users").json()
    assert any(u["username"] == "alice" for u in users)

    # Delete
    response = auth_server.delete(f"/api/users/{alice['id']}")
    assert response.status_code == 204

    users = auth_server.get("/api/users").json()
    assert not any(u["username"] == "alice" for u in users)


def test_cannot_delete_self(auth_server: httpx.Client) -> None:
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})
    me = auth_server.get("/api/me").json()

    response = auth_server.delete(f"/api/users/{me['id']}")
    assert response.status_code == 400
    assert "不能删除自己" in response.json()["detail"]


def test_change_password(auth_server: httpx.Client) -> None:
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})

    # Create a user
    user = auth_server.post("/api/users", json={"username": "dave"}).json()

    # Admin can change dave's password
    response = auth_server.post(
        f"/api/users/{user['id']}/password", json={"password": "newpass"}
    )
    assert response.status_code == 200
    assert response.json()["must_change_password"] is False

    # Log in with new password
    auth_server.cookies.clear()
    response = auth_server.post(
        "/api/login", json={"username": "dave", "password": "newpass"}
    )
    assert response.status_code == 204

    # Old password no longer works
    auth_server.cookies.clear()
    response = auth_server.post(
        "/api/login", json={"username": "dave", "password": "123456"}
    )
    assert response.status_code == 401


def test_user_can_change_own_password(auth_server: httpx.Client) -> None:
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})

    # Create a user
    user = auth_server.post("/api/users", json={"username": "eve"}).json()

    # Log in as eve
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "eve", "password": "123456"})

    # eve can change her own password
    response = auth_server.post(
        f"/api/users/{user['id']}/password", json={"password": "evepass"}
    )
    assert response.status_code == 200


def test_audit_records_actor_and_params(auth_server: httpx.Client) -> None:
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})

    # Create a site
    auth_server.post("/api/sites", json={"name": "audit-test", "host": ""})

    # Check audit log
    audit = auth_server.get("/api/audit").json()
    assert len(audit) > 0

    # Most recent should be site.create
    latest = audit[0]
    assert latest["action"] == "site.create"
    assert latest["actor"] == "admin1"
    assert "audit-test" in latest["detail"]
    assert latest["params"]["name"] == "audit-test"


def test_audit_trims_to_100_rows(auth_server: httpx.Client) -> None:
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})

    # Create 120 sites to trigger audit trimming
    for i in range(120):
        auth_server.post("/api/sites", json={"name": f"site-{i}", "host": ""})

    # Audit should have at most 100 rows
    audit = auth_server.get("/api/audit?limit=500").json()
    assert len(audit) <= 100


def test_duplicate_username_rejected(auth_server: httpx.Client) -> None:
    auth_server.cookies.clear()
    auth_server.post("/api/login", json={"username": "admin1", "password": "123456"})

    auth_server.post("/api/users", json={"username": "frank"})
    response = auth_server.post("/api/users", json={"username": "frank"})
    assert response.status_code == 409
    assert "已存在" in response.json()["detail"]


def test_login_route_has_resolved_response_annotation() -> None:
    import importlib

    # `poly_control_plane/__init__.py` re-exports the FastAPI instance as
    # `app`, which shadows the submodule; importlib gets the real module.
    app_module = importlib.import_module("poly_control_plane.app")

    route = next(r for r in app_module.app.routes if getattr(r, "path", "") == "/api/login")
    assert route.response_model is None
    assert app_module.login.__annotations__.get("return") is None
