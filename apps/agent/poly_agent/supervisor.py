"""Local supervisor: one OS process per simulator instance.

The supervisor is the only component that knows how to start things on this
machine. In this build it runs embedded in the control plane (single-machine
deployment); the same class is what a standalone agent will export for remote
sites, so nothing here may depend on FastAPI or the database.

**The control plane is not the data plane.** A PLC client talks directly to the
runtime's protocol port, so a runtime has no reason to die when the UI does.
`shutdown()` therefore detaches: it stops monitoring, closes log handles and
writes a manifest, but leaves the processes listening. On the next start
`_adopt()` reattaches to runtimes it can still identify instead of killing them
and losing their in-memory data. `stop_all()` is the explicit "really stop them"
path.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import psutil

from poly_contracts import RuntimeState, ServerConfig, config_fingerprint
from poly_core import can_bind


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _runtime_command() -> tuple[str, dict[str, str]]:
    """Executable and extra environment for one runtime process.

    On Windows a uv-managed venv's `python.exe` is a small launcher that spawns
    the real interpreter as a child. `Popen.pid` would then be the launcher,
    while `/state.pid` - and the process that owns the protocol port - is the
    child. Adoption and termination need the real pid, so run the base
    interpreter directly with the venv's site-packages on PYTHONPATH. A venv
    created by `python -m venv` has no launcher, and there `sys.executable`
    already is the real process.
    """
    exe = sys.executable
    extra: dict[str, str] = {}
    if sys.platform == "win32":
        base = getattr(sys, "_base_executable", "")
        site = Path(sys.prefix) / "Lib" / "site-packages"
        if base and Path(base) != Path(exe) and site.is_dir():
            # Keep every import path the control plane itself uses: the base
            # interpreter knows nothing about the venv's site-packages.
            paths = [str(site)]
            for entry in sys.path:
                if entry and entry not in paths:
                    paths.append(entry)
            exe = base
            extra["PYTHONPATH"] = os.pathsep.join(paths)
    return exe, extra


def check_bind(host: str, port: int) -> None:
    """Fail fast with a readable message instead of a runtime crash later.

    Deliberately the same probe the control plane runs and the same options the
    adapter will bind with: a check that uses different socket options than the
    listener answers a different question (see `poly_core.net`).
    """
    problem = can_bind(host, port)
    if problem:
        raise RuntimeError(f"无法绑定 {host}:{port} - {problem}")


def _process_alive(pid: int, create_time: float = 0.0) -> bool:
    """Is this still the process the manifest recorded?

    Pids get recycled. A bare `pid_exists` would happily adopt (or kill) an
    unrelated process that inherited the number, so the creation time is part
    of the identity. `0.0` means "unknown" (legacy manifest) and only checks
    liveness.
    """
    if pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        if create_time and abs(proc.create_time() - create_time) > 0.1:
            return False
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _looks_like_runtime(pid: int, config_path: Path) -> bool:
    """Does this pid run *this* simulator with *this* config file?

    Used for manifest records that predate `create_time` (legacy `{id: pid}`).
    Without an identity check, a pid recycled after a reboot could be killed
    just because it inherited a number. The command line is specific enough:
    our runtime is always `python -m poly_sim_runtime --config <path>`.
    """
    if pid <= 0:
        return False
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return any("poly_sim_runtime" in part for part in cmdline) and str(config_path) in cmdline


def _terminate_pid(pid: int, create_time: float = 0.0, timeout: float = 4.0) -> None:
    """Stop a process, but never one whose pid has been recycled."""
    if not _process_alive(pid, create_time):
        return
    try:
        proc = psutil.Process(pid)
        if create_time and abs(proc.create_time() - create_time) > 0.1:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except psutil.TimeoutExpired:
                pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


@dataclass
class RunRecord:
    """What the manifest remembers about one runtime beyond its pid.

    It is deliberately more than a pid file: adoption needs to answer "is this
    the same process?" (`create_time`) and "is it still running the config we
    think it is?" (`config_hash`, checked against the runtime's own `/state`).
    """

    pid: int
    create_time: float = 0.0
    control_port: int = 0
    config_hash: str = ""
    started_at: float | None = None
    protocol: str = ""
    bind_ip: str = ""
    port: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> RunRecord | None:
        if isinstance(raw, int):
            # Manifest written before adoption existed: only a pid. It cannot
            # be adopted safely, but it is still a runtime this data dir owned.
            return cls(pid=raw)
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                pid=int(raw.get("pid") or 0),
                create_time=float(raw.get("create_time") or 0.0),
                control_port=int(raw.get("control_port") or 0),
                config_hash=str(raw.get("config_hash") or ""),
                started_at=raw.get("started_at"),
                protocol=str(raw.get("protocol") or ""),
                bind_ip=str(raw.get("bind_ip") or ""),
                port=int(raw.get("port") or 0),
            )
        except (TypeError, ValueError):
            return None

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "create_time": self.create_time,
            "control_port": self.control_port,
            "config_hash": self.config_hash,
            "started_at": self.started_at,
            "protocol": self.protocol,
            "bind_ip": self.bind_ip,
            "port": self.port,
        }


@dataclass
class Managed:
    config: ServerConfig
    proc: subprocess.Popen | None = None
    pid: int = 0
    create_time: float = 0.0
    adopted: bool = False
    control_port: int = 0
    status: str = "stopped"
    error: str | None = None
    started_at: float | None = None
    values: dict[str, Any] = field(default_factory=dict)
    clients: int = 0
    endpoint: str | None = None
    config_hash: str = ""
    last_poll: float = 0.0
    log_handle: Any = None


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
        # Load, do not kill: the previous control plane may have left healthy
        # runtimes behind on purpose. `start()` decides whether each record can
        # be adopted; `reap_unwanted()` removes the ones the database no longer
        # wants.
        self._orphans: dict[str, RunRecord] = self._read_manifest()
        self._monitor = threading.Thread(target=self._monitor_loop, name="supervisor", daemon=True)
        self._monitor.start()

    # ------------------------------------------------------------------ orphans
    def _read_manifest(self) -> dict[str, RunRecord]:
        try:
            raw = json.loads(self.pid_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        records: dict[str, RunRecord] = {}
        for key, value in raw.items():
            record = RunRecord.from_json(value)
            if record is not None and record.pid:
                records[str(key)] = record
        return records

    def _record_alive(self, managed: Managed) -> bool:
        if managed.proc is not None:
            return managed.proc.poll() is None
        return _process_alive(managed.pid, managed.create_time)

    def _write_manifest(self) -> None:
        records: dict[str, RunRecord] = {}
        for key, managed in self._servers.items():
            if not self._record_alive(managed):
                continue
            records[key] = RunRecord(
                pid=managed.pid,
                create_time=managed.create_time,
                control_port=managed.control_port,
                config_hash=managed.config_hash,
                started_at=managed.started_at,
                protocol=managed.config.protocol.value,
                bind_ip=managed.config.bind_ip,
                port=managed.config.port,
            )
        # Keep records that have not been reconciled yet. In the normal startup
        # flow there are none, but losing one would orphan a live process.
        for key, record in self._orphans.items():
            records.setdefault(key, record)
        try:
            tmp = self.pid_file.with_name(self.pid_file.name + ".tmp")
            payload = json.dumps(
                {key: record.to_json() for key, record in records.items()},
                indent=2,
            )
            # A half-written manifest is worse than an old one: the next start
            # would neither adopt nor be able to clean up. Replace atomically.
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.pid_file)
        except OSError:
            pass

    # ------------------------------------------------------------------ public
    def hash_config(self, config: ServerConfig) -> str:
        return config_fingerprint(config)

    def start(self, config: ServerConfig) -> RuntimeState:
        with self._lock:
            managed = self._servers.get(config.id)
            if managed and self._record_alive(managed):
                if managed.config_hash == self.hash_config(config):
                    return self.state(config.id)
                self._stop_locked(managed)
            record = self._orphans.pop(config.id, None)
            if record is not None and self._adopt(config, record):
                return self.state(config.id)
            managed = Managed(config=config, config_hash=self.hash_config(config))
            self._servers[config.id] = managed
            try:
                check_bind(config.bind_ip, config.port)
                self._spawn(managed)
            except Exception as exc:
                managed.status = "error"
                managed.error = f"{type(exc).__name__}: {exc}"
                self._write_manifest()
                raise
            return self.state(config.id)

    def stop(self, server_id: str) -> None:
        with self._lock:
            managed = self._servers.get(server_id)
            if managed is not None:
                self._stop_locked(managed)
                self._servers.pop(server_id, None)
            # It may still be an un-reconciled orphan (stop before startup
            # finished); honour the request for that case too.
            record = self._orphans.pop(server_id, None)
            if record is not None:
                self._terminate_record(server_id, record)
            self._write_manifest()

    def restart(self, config: ServerConfig) -> RuntimeState:
        self.stop(config.id)
        return self.start(config)

    def forget(self, server_id: str) -> None:
        """Stop a server that no longer exists in the desired state."""
        self.stop(server_id)

    def stop_all(self) -> None:
        """Explicitly stop every runtime this data dir knows about.

        This is what an operator asks for when they really want the ports free
        (uninstall, switching machines, a destructive test). A normal control
        plane exit does *not* call it.
        """
        with self._lock:
            for managed in list(self._servers.values()):
                self._stop_locked(managed)
            self._servers.clear()
            for server_id, record in list(self._orphans.items()):
                self._terminate_record(server_id, record)
            self._orphans.clear()
            self._write_manifest()

    def reap_unwanted(self, keep_ids: set[str]) -> None:
        """Stop runtimes the desired state no longer wants.

        Called once at startup with the enabled local simulator ids. A disabled
        or deleted instance must not keep listening just because it happened to
        run when the previous control plane went down.
        """
        with self._lock:
            for server_id, record in list(self._orphans.items()):
                if server_id in keep_ids:
                    continue
                self._terminate_record(server_id, record)
                self._orphans.pop(server_id, None)
            for server_id, managed in list(self._servers.items()):
                if server_id not in keep_ids:
                    self._stop_locked(managed)
                    self._servers.pop(server_id, None)
            self._write_manifest()

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
            trust_env=False,
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
            trust_env=False,
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
            trust_env=False,
        )
        if response.status_code >= 400:
            detail = response.json().get("detail", response.text)
            raise ValueError(detail)

    def connections(self, server_id: str) -> list[dict[str, Any]]:
        managed = self._servers.get(server_id)
        if managed is None or managed.status != "running":
            raise RuntimeError("实例未在运行")
        response = httpx.get(
            f"http://127.0.0.1:{managed.control_port}/connections",
            timeout=3.0,
            trust_env=False,
        )
        if response.status_code >= 400:
            raise ValueError(response.json().get("detail", response.text))
        return list(response.json().get("connections", []))

    def kick_connection(self, server_id: str, conn_id: str) -> None:
        managed = self._servers.get(server_id)
        if managed is None or managed.status != "running":
            raise RuntimeError("实例未在运行")
        response = httpx.post(
            f"http://127.0.0.1:{managed.control_port}/connections/{quote(conn_id, safe='')}/kick",
            timeout=3.0,
            trust_env=False,
        )
        if response.status_code >= 400:
            raise ValueError(response.json().get("detail", response.text))

    def reset(self, server_id: str) -> None:
        managed = self._servers.get(server_id)
        if managed is None or managed.status != "running":
            raise RuntimeError("实例未在运行")
        response = httpx.post(
            f"http://127.0.0.1:{managed.control_port}/reset",
            timeout=3.0,
            trust_env=False,
        )
        if response.status_code == 404:
            # The runtime process predates the reset endpoint (adopted from an
            # older control plane); only a restart can bring it up to date.
            raise ValueError("当前 runtime 版本过旧，无法原地清零；请先「重启」实例再重试")
        if response.status_code >= 400:
            raise ValueError(response.json().get("detail", response.text))

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

    def shutdown(self, stop_instances: bool = False) -> None:
        """Detach from the runtimes, or stop them when explicitly asked.

        The default is deliberately non-destructive: `enabled` is the desired
        state, and a control plane restart should reconcile against processes
        that never stopped. Code that really wants them gone (tests, an operator
        tearing the machine down) calls `stop_all()` or passes
        `stop_instances=True`.
        """
        self._stop.set()
        with self._lock:
            if stop_instances:
                for managed in list(self._servers.values()):
                    self._stop_locked(managed)
                for server_id, record in list(self._orphans.items()):
                    self._terminate_record(server_id, record)
                self._servers.clear()
                self._orphans.clear()
                self._write_manifest()
            else:
                for managed in self._servers.values():
                    self._close_log(managed)
                self._write_manifest()
                self._servers.clear()
                self._orphans.clear()

    # ------------------------------------------------------------------ internals
    def _terminate_record(self, server_id: str, record: RunRecord) -> None:
        """Stop an orphan, but only one we can identify.

        A record from the pre-adoption manifest has a pid and nothing else. Its
        process is normally dead after a reboot, and the pid may by then belong
        to something unrelated; killing it would be worse than leaving the old
        runtime to be reported as a port conflict.
        """
        if record.create_time:
            _terminate_pid(record.pid, record.create_time)
        elif _looks_like_runtime(record.pid, self.config_dir / f"{server_id}.json"):
            _terminate_pid(record.pid, 0.0)

    def _adopt(self, config: ServerConfig, record: RunRecord) -> bool:
        """Attach to a runtime left behind by the previous control plane.

        Adoption is narrow on purpose: the process must still be the one the
        manifest named (pid + creation time), it must serve `/state` on its
        control port, and that state must report the same instance and the same
        config fingerprint. Everything else - dead/recycled pid, changed port
        or tags, hung control server, adapter in error - is stopped and started
        fresh. The promise is "a healthy runtime with the same config survives a
        control-plane restart", not "bring back any process that answers".
        """
        if not _process_alive(record.pid, record.create_time):
            return False
        if not record.control_port or not record.config_hash:
            # Legacy manifest (pid only): cannot identify it, so do not adopt.
            self._terminate_record(config.id, record)
            return False
        if record.config_hash != self.hash_config(config):
            # The database changed while the control plane was away (or this is
            # the first start after an upgrade that changed the fingerprint).
            self._terminate_record(config.id, record)
            return False
        try:
            response = httpx.get(
                f"http://127.0.0.1:{record.control_port}/state", timeout=1.5, trust_env=False
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            # Alive but not answering: either hung or still inside adapter
            # startup (its data area has not been established yet). Replacing
            # it is safer than showing a corpse as running.
            self._terminate_record(config.id, record)
            return False
        try:
            reported_pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            reported_pid = 0
        if not record.create_time and reported_pid != record.pid:
            # No creation time: the pid check is the only identity we have, so
            # require the runtime to confirm it explicitly.
            self._terminate_record(config.id, record)
            return False
        if (
            data.get("server_id") != config.id
            or data.get("config_hash") != record.config_hash
            or (reported_pid and reported_pid != record.pid)
        ):
            self._terminate_record(config.id, record)
            return False
        status = str(data.get("status") or "")
        if status not in ("running", "starting"):
            self._terminate_record(config.id, record)
            return False
        self._servers[config.id] = Managed(
            config=config,
            pid=record.pid,
            create_time=record.create_time,
            adopted=True,
            control_port=record.control_port,
            status=status,
            started_at=data.get("started_at") or record.started_at,
            config_hash=record.config_hash,
        )
        self._write_manifest()
        return True

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
        managed.log_handle = log_handle

        exe, extra_env = _runtime_command()
        env = os.environ.copy()
        env.update(extra_env)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        command = [
            exe,
            "-m",
            "poly_sim_runtime",
            "--config",
            str(config_path),
            "--control-port",
            str(managed.control_port),
        ]
        common: dict[str, Any] = {
            "cwd": str(self.root),
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": env,
        }
        try:
            if sys.platform == "win32":
                # CREATE_NO_WINDOW keeps the runtime off the control plane's
                # console. CREATE_BREAKAWAY_FROM_JOB is a best effort escape
                # from an IDE/service-manager job object that would otherwise
                # kill the whole tree when the control plane exits - exactly
                # the "abnormal exit" the simulator must survive. Jobs that
                # forbid breakaway answer ERROR_ACCESS_DENIED; fall back rather
                # than refusing to start at all.
                try:
                    managed.proc = subprocess.Popen(
                        command,
                        creationflags=0x08000000 | 0x01000000,
                        **common,
                    )
                except OSError as exc:
                    if getattr(exc, "winerror", None) != 5:
                        raise
                    managed.proc = subprocess.Popen(
                        command, creationflags=0x08000000, **common
                    )
            else:
                # Own session: Ctrl+C in the control plane's terminal goes to
                # the control plane's process group, not to the simulator.
                # Without it, a "graceful" stop on POSIX would still take the
                # runtimes down.
                managed.proc = subprocess.Popen(command, start_new_session=True, **common)
        except Exception:
            self._close_log(managed)
            raise
        managed.pid = managed.proc.pid
        try:
            managed.create_time = psutil.Process(managed.pid).create_time()
        except psutil.Error:
            managed.create_time = 0.0
        managed.status = "starting"
        managed.error = None
        managed.started_at = time.time()
        managed.last_poll = 0.0
        self._write_manifest()

    def _close_log(self, managed: Managed) -> None:
        if managed.log_handle is not None:
            try:
                managed.log_handle.close()
            except Exception:
                pass
            managed.log_handle = None

    def _stop_locked(self, managed: Managed) -> None:
        proc = managed.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            except Exception:
                pass
        elif proc is None and managed.pid:
            if managed.create_time:
                _terminate_pid(managed.pid, managed.create_time)
            elif _looks_like_runtime(
                managed.pid, self.config_dir / f"{managed.config.id}.json"
            ):
                _terminate_pid(managed.pid, 0.0)
        managed.proc = None
        managed.pid = 0
        managed.status = "stopped"
        self._close_log(managed)
        self._write_manifest()

    def _poll_runtime(self, managed: Managed) -> None:
        if not self._record_alive(managed):
            managed.status = "error"
            managed.values = {}
            if not managed.error:
                if managed.proc is not None:
                    tail = self.logs(managed.config.id, lines=8).strip().splitlines()
                    managed.error = tail[-1] if tail else f"进程已退出 (code={managed.proc.returncode})"
                else:
                    managed.error = "运行时进程已退出"
            return
        try:
            response = httpx.get(
                f"http://127.0.0.1:{managed.control_port}/state", timeout=1.5, trust_env=False
            )
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
        try:
            reported_pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            reported_pid = 0
        if reported_pid and managed.pid and reported_pid != managed.pid:
            managed.status = "error"
            managed.error = f"控制端口 {managed.control_port} 已被其他进程占用"
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
            pid=managed.pid,
            config_hash=managed.config_hash,
        )

    def _monitor_loop(self) -> None:
        while not self._stop.wait(1.5):
            with self._lock:
                for managed in list(self._servers.values()):
                    if not self._stop.is_set():
                        self._poll_runtime(managed)
