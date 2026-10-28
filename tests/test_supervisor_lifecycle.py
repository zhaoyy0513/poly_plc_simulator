"""The control plane may restart; the data plane must not.

These tests use real Modbus runtimes because the whole point is a real process
that keeps its in-memory data area. A unit test with a fake Popen would pass
even if the runtime were killed and rebuilt.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import psutil

from poly_agent import Supervisor
from poly_contracts import Protocol, ServerConfig, ServerOptions

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def config(port: int | None = None) -> ServerConfig:
    return ServerConfig(
        id="lifecycle-modbus",
        site_id="site",
        name="lifecycle",
        protocol=Protocol.MODBUS_TCP,
        bind_ip="127.0.0.1",
        port=port or free_port(),
        options=ServerOptions(),
        tags=[],
    )


def listening(port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.3)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def wait_running(sup: Supervisor, server_id: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = sup.state(server_id)
        if state.status == "running":
            return
        if state.status == "error":
            raise AssertionError(state.error)
        time.sleep(0.15)
    raise AssertionError("runtime did not start")


def wait_gone(pid: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not psutil.pid_exists(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"pid {pid} is still alive")


def cleanup(data_dir: Path) -> None:
    """Best-effort cleanup so a failing assertion cannot leak a runtime."""
    try:
        sup = Supervisor(data_dir, ROOT)
        sup.reap_unwanted(set())
        sup.shutdown()
    except Exception:
        pass


def test_detach_then_adopt_keeps_pid_data_and_start_time(tmp_path: Path) -> None:
    port = free_port()
    cfg = config(port)
    try:
        sup = Supervisor(tmp_path, ROOT)
        sup.start(cfg)
        wait_running(sup, cfg.id)
        sup.write_area(cfg.id, "holding", 0, 4321)
        pid = sup._servers[cfg.id].pid
        started_at = sup.state(cfg.id).started_at

        # What the control plane does on a normal exit: stop monitoring, keep
        # the process and its data area alive.
        sup.shutdown()
        assert listening(port)

        second = Supervisor(tmp_path, ROOT)
        state = second.start(cfg)
        assert state.status == "running"
        assert state.started_at == started_at
        managed = second._servers[cfg.id]
        assert managed.adopted is True
        assert managed.pid == pid
        assert second.read_area(cfg.id, "holding", 0, 1) == [4321]
        second.stop_all()
    finally:
        cleanup(tmp_path)


def test_reap_unwanted_stops_a_disabled_orphan(tmp_path: Path) -> None:
    port = free_port()
    cfg = config(port)
    try:
        sup = Supervisor(tmp_path, ROOT)
        sup.start(cfg)
        wait_running(sup, cfg.id)
        pid = sup._servers[cfg.id].pid
        sup.shutdown()
        assert listening(port)

        # The database says this instance is disabled now. A control plane
        # start must not leave it holding the port.
        second = Supervisor(tmp_path, ROOT)
        second.reap_unwanted(set())
        assert not listening(port)
        wait_gone(pid)
        second.shutdown()
    finally:
        cleanup(tmp_path)


def test_a_changed_config_replaces_the_old_runtime(tmp_path: Path) -> None:
    old_port = free_port()
    cfg = config(old_port)
    try:
        sup = Supervisor(tmp_path, ROOT)
        sup.start(cfg)
        wait_running(sup, cfg.id)
        sup.write_area(cfg.id, "holding", 0, 777)
        old_pid = sup._servers[cfg.id].pid
        sup.shutdown()

        new_port = free_port()
        changed = cfg.model_copy(update={"port": new_port})
        second = Supervisor(tmp_path, ROOT)
        second.start(changed)
        wait_running(second, changed.id)
        managed = second._servers[changed.id]
        assert managed.adopted is False
        assert managed.pid != old_pid
        assert listening(new_port)
        assert not listening(old_port)
        # The replaced runtime starts from a clean data area.
        assert second.read_area(changed.id, "holding", 0, 1) == [0]
        second.stop_all()
    finally:
        cleanup(tmp_path)


def test_a_legacy_pid_only_manifest_is_not_adopted(tmp_path: Path) -> None:
    port = free_port()
    cfg = config(port)
    try:
        sup = Supervisor(tmp_path, ROOT)
        sup.start(cfg)
        wait_running(sup, cfg.id)
        sup.write_area(cfg.id, "holding", 0, 999)
        old_pid = sup._servers[cfg.id].pid
        sup.shutdown()

        # Pre-adoption builds wrote {id: pid}; there is no control port or
        # fingerprint, so the process cannot be identified safely.
        (tmp_path / "runtimes.json").write_text(
            json.dumps({cfg.id: old_pid}), encoding="utf-8"
        )

        second = Supervisor(tmp_path, ROOT)
        second.start(cfg)
        wait_running(second, cfg.id)
        assert second._servers[cfg.id].adopted is False
        assert second._servers[cfg.id].pid != old_pid
        assert second.read_area(cfg.id, "holding", 0, 1) == [0]
        wait_gone(old_pid)
        second.stop_all()
    finally:
        cleanup(tmp_path)


def test_a_legacy_manifest_does_not_kill_an_unrelated_pid(tmp_path: Path) -> None:
    # A pid-only record survives a reboot. By then the number may belong to a
    # completely different process; killing it because it "was in the manifest"
    # would be unacceptable. Only a matching runtime command line may be stopped.
    cfg = config()
    (tmp_path / "runtimes.json").write_text(
        json.dumps({cfg.id: os.getpid()}), encoding="utf-8"
    )
    try:
        sup = Supervisor(tmp_path, ROOT)
        sup.start(cfg)
        wait_running(sup, cfg.id)
        assert psutil.pid_exists(os.getpid())
        sup.stop_all()
    finally:
        cleanup(tmp_path)


def test_a_corrupt_manifest_does_not_block_startup(tmp_path: Path) -> None:
    (tmp_path / "runtimes.json").write_text("{not json", encoding="utf-8")
    cfg = config()
    try:
        sup = Supervisor(tmp_path, ROOT)
        sup.start(cfg)
        wait_running(sup, cfg.id)
        sup.stop_all()
    finally:
        cleanup(tmp_path)


def test_stop_instances_is_the_explicit_destructive_path(tmp_path: Path) -> None:
    port = free_port()
    cfg = config(port)
    try:
        sup = Supervisor(tmp_path, ROOT)
        sup.start(cfg)
        wait_running(sup, cfg.id)
        pid = sup._servers[cfg.id].pid
        # Used by --stop-on-exit: an operator really does want the ports free.
        sup.shutdown(stop_instances=True)
        assert not listening(port)
        wait_gone(pid)
        assert json.loads((tmp_path / "runtimes.json").read_text(encoding="utf-8")) == {}
    finally:
        cleanup(tmp_path)
