"""Local supervisor: one OS process per simulator instance.

The supervisor is the only component that knows how to start things on this
machine. In this build it runs embedded in the control plane (single-machine
deployment); the same class is what a standalone agent will export for remote
sites, so nothing here may depend on FastAPI or the database.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from poly_contracts import RuntimeState, ServerConfig


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def check_bind(host: str, port: int) -> None:
    """Fail fast with a readable message instead of a runtime crash later."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
    except OSError as exc:
        raise RuntimeError(f"无法绑定 {host}:{port} - {exc.strerror or exc}") from exc
    finally:
        probe.close()


@dataclass
class Managed:
    config: ServerConfig
    proc: subprocess.Popen | None = None
    control_port: int = 0
    status: str = "stopped"
    error: str | None = None
    started_at: float | None = None
    values: dict[str, Any] = field(default_factory=dict)
    clients: int = 0
    endpoint: str | None = None
    config_hash: str = ""
    last_poll: float = 0.0


class Supervisor:
    def __init__(self, data_dir: str | Path, root: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = Path(root)
        self.log_dir = self.data_dir / "logs"
        self.config_dir = self.data_dir / "configs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file = self.data_dir / "runtimes.json"
        self._servers: dict[str, Managed] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._kill_stale()
        self._monitor = threading.Thread(target=self._monitor_loop, name="supervisor", daemon=True)
        self._monitor.start()

    # ------------------------------------------------------------------ orphans
    def _read_pids(self) -> dict[str, int]:
        try:
            return json.loads(self.pid_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_pids(self) -> None:
        pids = {
            key: managed.proc.pid
            for key, managed in self._servers.items()
            if managed.proc is not None and managed.proc.poll() is None
        }
        try:
            self.pid_file.write_text(json.dumps(pids, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _kill_stale(self) -> None:
        """Reap runtimes left behind by a hard-killed control plane.

        Runtimes are independent processes by design, so a SIGKILL of this
        process leaves them listening. Without this, the next start would fail
        on "port already in use" with no way to recover from the UI.
        """
        stale = self._read_pids()
        for pid in stale.values():
            if not pid or pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        if stale:
            try:
                self.pid_file.write_text("{}", encoding="utf-8")
            except OSError:
                pass

    # ------------------------------------------------------------------ public
    def hash_config(self, config: ServerConfig) -> str:
        return json.dumps(config.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)

    def start(self, config: ServerConfig) -> RuntimeState:
        with self._lock:
            managed = self._servers.get(config.id)
            if managed and managed.proc and managed.proc.poll() is None:
                if managed.config_hash == self.hash_config(config):
                    return self.state(config.id)
                self._stop_locked(managed)
            managed = Managed(config=config, config_hash=self.hash_config(config))
            self._servers[config.id] = managed
            try:
                check_bind(config.bind_ip, config.port)
                self._spawn(managed)
            except Exception as exc:
                managed.status = "error"
                managed.error = f"{type(exc).__name__}: {exc}"
                raise
            return self.state(config.id)

    def stop(self, server_id: str) -> None:
        with self._lock:
            managed = self._servers.get(server_id)
            if managed is None:
                return
            self._stop_locked(managed)
            self._servers.pop(server_id, None)

    def restart(self, config: ServerConfig) -> RuntimeState:
        self.stop(config.id)
        return self.start(config)

    def forget(self, server_id: str) -> None:
        """Stop a server that no longer exists in the desired state."""
        self.stop(server_id)

    def state(self, server_id: str) -> RuntimeState:
        with self._lock:
            managed = self._servers.get(server_id)
            if managed is None:
                return RuntimeState(server_id=server_id, status="stopped")
            self._refresh(managed)
            return self._to_state(managed)

    def states(self) -> dict[str, RuntimeState]:
        with self._lock:
            for managed in self._servers.values():
                self._refresh(managed)
            return {key: self._to_state(value) for key, value in self._servers.items()}

    def values(self, server_id: str) -> RuntimeState:
        with self._lock:
            managed = self._servers.get(server_id)
            if managed is None:
                return RuntimeState(server_id=server_id, status="stopped")
            self._poll_runtime(managed)
            return self._to_state(managed)

    def write(self, server_id: str, tag: str, value: Any) -> None:
        managed = self._servers.get(server_id)
        if managed is None or managed.status != "running":
            raise RuntimeError("实例未在运行")
        response = httpx.post(
            f"http://127.0.0.1:{managed.control_port}/write",
            json={"tag": tag, "value": value},
            timeout=3.0,
        )
        if response.status_code >= 400:
            detail = response.json().get("detail", response.text)
            raise ValueError(detail)

    def read_area(self, server_id: str, area: str, address: int, count: int) -> list[Any]:
        managed = self._servers.get(server_id)
        if managed is None or managed.status != "running":
            raise RuntimeError("实例未在运行")
        response = httpx.get(
            f"http://127.0.0.1:{managed.control_port}/area",
            params={"area": area, "address": address, "count": count},
            timeout=3.0,
        )
        if response.status_code >= 400:
            detail = response.json().get("detail", response.text)
            raise ValueError(detail)
        return list(response.json().get("values", []))

    def write_area(self, server_id: str, area: str, address: int, value: Any) -> None:
        managed = self._servers.get(server_id)
        if managed is None or managed.status != "running":
            raise RuntimeError("实例未在运行")
        response = httpx.post(
            f"http://127.0.0.1:{managed.control_port}/area/write",
            json={"area": area, "address": address, "value": value},
            timeout=3.0,
        )
        if response.status_code >= 400:
            detail = response.json().get("detail", response.text)
            raise ValueError(detail)

    def logs(self, server_id: str, lines: int = 200) -> str:
        path = self.log_dir / f"{server_id}.log"
        if not path.exists():
            return ""
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 128 * 1024))
                data = handle.read().decode("utf-8", errors="replace")
        except OSError as exc:
            return f"<无法读取日志: {exc}>"
        return "\n".join(data.splitlines()[-lines:])

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for managed in list(self._servers.values()):
                self._stop_locked(managed)
            self._servers.clear()

    # ------------------------------------------------------------------ internals
    def _spawn(self, managed: Managed) -> None:
        config = managed.config
        # A remote runtime has an assigned port so the platform can reach it
        # again through SSH; local ones just take whatever is free.
        managed.control_port = config.control_port or _free_port()
        config_path = self.config_dir / f"{config.id}.json"
        config_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
        log_path = self.log_dir / f"{config.id}.log"
        log_handle = log_path.open("ab")
        header = (
            f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"{config.protocol.value} {config.bind_ip}:{config.port} =====\n"
        )
        log_handle.write(header.encode("utf-8"))
        log_handle.flush()
        managed.log_handle = log_handle  # type: ignore[attr-defined]

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        managed.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "poly_sim_runtime",
                "--config",
                str(config_path),
                "--control-port",
                str(managed.control_port),
            ],
            cwd=str(self.root),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=flags,
        )
        managed.status = "starting"
        managed.error = None
        managed.started_at = time.time()
        managed.last_poll = 0.0
        self._write_pids()

    def _stop_locked(self, managed: Managed) -> None:
        proc = managed.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass
        managed.proc = None
        managed.status = "stopped"
        handle = getattr(managed, "log_handle", None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
            managed.log_handle = None  # type: ignore[attr-defined]
        self._write_pids()

    def _poll_runtime(self, managed: Managed) -> None:
        if managed.proc is None:
            managed.status = "stopped"
            return
        if managed.proc.poll() is not None:
            managed.status = "error"
            managed.values = {}
            if not managed.error:
                tail = self.logs(managed.config.id, lines=8).strip().splitlines()
                managed.error = tail[-1] if tail else f"进程已退出 (code={managed.proc.returncode})"
            return
        try:
            response = httpx.get(f"http://127.0.0.1:{managed.control_port}/state", timeout=1.5)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            # A slow adapter (OPC UA takes ~2 s to build its address space) must
            # not be reported as broken while it is still inside the start grace
            # window. Only a runtime that stays silent past it is an error.
            age = time.time() - (managed.started_at or 0)
            if managed.status == "starting" and age < 25:
                return
            if managed.status != "running":
                managed.status = "error"
                managed.error = f"运行时无响应: {exc}"
            return
        managed.status = data.get("status", "running")
        if managed.status == "running":
            managed.error = None
        elif data.get("error"):
            managed.error = str(data["error"])
        managed.values = data.get("values") or {}
        managed.clients = int(data.get("clients") or 0)
        managed.endpoint = data.get("endpoint")
        managed.started_at = data.get("started_at") or managed.started_at
        managed.last_poll = time.time()

    def _refresh(self, managed: Managed) -> None:
        # Values are cheap to fetch locally but polling every caller would multiply
        # requests; 400 ms keeps the UI fresh without hammering the runtime.
        if time.time() - managed.last_poll < 0.4 and managed.status == "running":
            return
        self._poll_runtime(managed)

    def _to_state(self, managed: Managed) -> RuntimeState:
        return RuntimeState(
            server_id=managed.config.id,
            status=managed.status,  # type: ignore[arg-type]
            started_at=managed.started_at,
            error=managed.error,
            values=managed.values,
            clients=managed.clients,
            endpoint=managed.endpoint,
        )

    def _monitor_loop(self) -> None:
        while not self._stop.wait(1.5):
            with self._lock:
                for managed in list(self._servers.values()):
                    if not self._stop.is_set():
                        self._poll_runtime(managed)
