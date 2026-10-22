"""SSH access to a target machine.

Nothing here is installed on the far side: we connect, run a command, and
leave. That is the whole point of the chosen deployment model - a site keeps
its simulator processes, not a daemon of ours.

One connection per site is reused (see `SshSessionPool`) because the UI polls
every second; opening a fresh SSH handshake each time would be wasteful.
"""

from __future__ import annotations

import platform
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import paramiko

from poly_contracts import SiteProbe

from .system import data_dir

# Deploy paths are interpolated into a shell command, so keep them boring.
_SAFE_PATH = re.compile(r"^[\w./\-~]*$")
DEFAULT_DEPLOY_DIR = "~/.poly-sim"


@dataclass
class SshTarget:
    host: str
    port: int = 22
    user: str = ""
    password: str = ""
    timeout: float = 10.0


class SshRunner(Protocol):
    def run(self, command: str, timeout: float = 10.0) -> tuple[int, str, str]: ...

    def close(self) -> None: ...


class SshError(RuntimeError):
    """Anything that stops us from talking to the machine."""


class ParamikoRunner:
    def __init__(self, target: SshTarget) -> None:
        self.target = target
        self._client = paramiko.SSHClient()
        # Auto-accepting unknown host keys keeps onboarding a machine down to
        # "type its password". It does mean a first-contact MITM is possible;
        # revisit before this ever runs outside a trusted network.
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._connect()

    def _connect(self) -> None:
        try:
            self._client.connect(
                hostname=self.target.host,
                port=self.target.port,
                username=self.target.user,
                password=self.target.password or None,
                timeout=self.target.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as exc:
            raise SshError(f"SSH 连接失败: {exc}") from exc

    def alive(self) -> bool:
        transport = self._client.get_transport()
        return bool(transport and transport.is_active())

    def run(self, command: str, timeout: float = 10.0) -> tuple[int, str, str]:
        if not self.alive():
            raise SshError("SSH 连接已断开")
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        return (
            stdout.channel.recv_exit_status(),
            stdout.read().decode("utf-8", "replace"),
            stderr.read().decode("utf-8", "replace"),
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


class SshSessionPool:
    """One live connection per site, reopened transparently when it dies."""

    def __init__(self) -> None:
        self._sessions: dict[str, ParamikoRunner] = {}
        self._lock = threading.RLock()

    def acquire(self, key: str, target: SshTarget) -> ParamikoRunner:
        with self._lock:
            current = self._sessions.get(key)
            if current is not None:
                if current.alive():
                    return current
                current.close()
            runner = ParamikoRunner(target)
            self._sessions[key] = runner
            return runner

    def drop(self, key: str) -> None:
        with self._lock:
            current = self._sessions.pop(key, None)
            if current is not None:
                current.close()

    def close_all(self) -> None:
        with self._lock:
            for runner in self._sessions.values():
                runner.close()
            self._sessions.clear()


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _parse_ports(text: str) -> list[int]:
    """Pull listening ports out of `ss -ltnH` / `netstat -ltn` output."""
    ports: set[int] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        candidate = parts[3].rsplit(":", 1)[-1]
        if candidate.isdigit():
            ports.add(int(candidate))
    return sorted(ports)


def _parse_os_release(text: str) -> str:
    """PRETTY_NAME out of /etc/os-release, e.g. "Ubuntu 22.04.3 LTS"."""
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def _command_paths(runner: SshRunner, candidates: tuple[str, ...]) -> list[str]:
    script = "for c in " + " ".join(candidates) + '; do command -v "$c" 2>/dev/null; done; true'
    _, out, _ = runner.run(script)
    return [line.strip() for line in out.splitlines() if line.strip()]


def probe_target(runner: SshRunner, deploy_dir: str = DEFAULT_DEPLOY_DIR) -> SiteProbe:
    checked = datetime.now(timezone.utc).isoformat()

    uname = _first_line(runner.run("uname -s -m")[1])
    os_name, _, arch = uname.partition(" ")

    _, os_release, _ = runner.run("cat /etc/os-release 2>/dev/null; true")
    distro = _parse_os_release(os_release)

    _, python_out, _ = runner.run("python3 --version 2>&1; true")
    python_bin = "python3" if "Python 3." in python_out else ""
    python_version = _first_line(python_out) if python_bin else ""

    python_candidates = _command_paths(
        runner, ("python3.13", "python3.12", "python3.11", "python3.10", "python3.9")
    )
    if python_candidates and not python_bin:
        # Old distros ship `python3` pointing at 3.5/3.6; a newer candidate is
        # the one we would actually use for the runtime.
        python_bin = Path(python_candidates[-1]).name
        _, out, _ = runner.run(f"{python_candidates[-1]} --version 2>&1; true")
        python_version = _first_line(out)

    package_manager = ""
    managers = _command_paths(runner, ("apt-get", "dnf", "yum", "zypper"))
    if managers:
        package_manager = Path(managers[0]).name

    _, docker_out, _ = runner.run("docker --version 2>/dev/null; true")
    docker = _first_line(docker_out)

    deploy_ok = False
    if _SAFE_PATH.match(deploy_dir or ""):
        code, out, _ = runner.run(f"mkdir -p -- {deploy_dir} && test -w {deploy_dir} && echo OK")
        deploy_ok = "OK" in out

    _, ports_out, _ = runner.run("ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null || true")

    return SiteProbe(
        ok=True,
        os=os_name or "unknown",
        arch=arch,
        distro=distro,
        python=python_version,
        python_bin=python_bin,
        python_candidates=python_candidates,
        package_manager=package_manager,
        docker=docker,
        deploy_dir=deploy_dir,
        deploy_ok=deploy_ok,
        ports_in_use=_parse_ports(ports_out),
        checked_at=checked,
    )


def run_probe(target: SshTarget, deploy_dir: str = DEFAULT_DEPLOY_DIR) -> SiteProbe:
    """Connect, probe, disconnect - what the 'test connection' button does."""
    runner = ParamikoRunner(target)
    try:
        return probe_target(runner, deploy_dir)
    finally:
        runner.close()


def probe_local(deploy_dir: str = "") -> SiteProbe:
    """A site with no host is this machine; there is nothing to connect to."""
    return SiteProbe(
        ok=True,
        os=platform.platform(),
        arch=platform.machine(),
        python=sys.version.split()[0],
        python_bin=sys.executable,
        deploy_dir=str(data_dir()),
        deploy_ok=True,
        ports_in_use=[],
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
