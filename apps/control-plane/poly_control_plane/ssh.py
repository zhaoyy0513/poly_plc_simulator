"""SSH access to a target machine.

Nothing here is installed on the far side: we connect, run a command, and
leave. That is the whole point of the chosen deployment model - a site keeps
its simulator processes, not a daemon of ours.

One connection per site is reused (see `SshSessionPool`) because the UI polls
every second; opening a fresh SSH handshake each time would be wasteful.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import paramiko

from poly_contracts import SiteProbe

from .system import data_dir

# Deploy paths are interpolated into a shell command, so keep them boring.
_SAFE_PATH = re.compile(r"^[\w./\-~]*$")
DEFAULT_DEPLOY_DIR = "~/.poly-sim"

# The simulator runtime needs 3.10+ (match statements, modern typing).
MIN_PYTHON = (3, 10)


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


# Exit code travels in stdout, not in the channel's exit-status message: see
# ParamikoRunner.run for why.
_RC_MARK = "__poly_rc="
_RC_RE = re.compile(rb"__poly_rc=(-?\d+)\s*$")


def _drain_channel(channel: paramiko.Channel, timeout: float) -> tuple[bytes, bytes]:
    """Read both streams until the command has reported back.

    Stops on EOF, on timeout, or - the usual case - as soon as the exit marker
    has arrived and nothing else is pending. A daemonised child may keep the
    channel open forever, so EOF cannot be the only exit condition.
    """
    out = bytearray()
    err = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        moved = False
        while channel.recv_ready():
            out.extend(channel.recv(65536))
            moved = True
        while channel.recv_stderr_ready():
            err.extend(channel.recv_stderr(65536))
            moved = True
        if not moved:
            if _RC_RE.search(bytes(out)) or channel.eof_received or channel.closed:
                break
            time.sleep(0.02)
    return bytes(out), bytes(err)


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

    @property
    def client(self) -> paramiko.SSHClient:
        return self._client

    def alive(self) -> bool:
        transport = self._client.get_transport()
        return bool(transport and transport.is_active())

    def run(self, command: str, timeout: float = 10.0) -> tuple[int, str, str]:
        """Run a command and collect its output without waiting for channel EOF.

        `recv_exit_status()` plus `stdout.read()` deadlocks as soon as a command
        leaves something behind: the background process keeps the channel open,
        EOF never arrives, and the read blocks until the socket times out. So
        the exit code is carried in-band instead, and the reader stops as soon
        as it has seen it.
        """
        if not self.alive():
            raise SshError("SSH 连接已断开")
        transport = self._client.get_transport()
        if transport is None:
            raise SshError("SSH 连接已断开")
        try:
            channel = transport.open_session(timeout=timeout)
        except Exception as exc:
            raise SshError(f"无法打开 SSH 会话: {exc}") from exc
        channel.settimeout(timeout)
        wrapped = f"(\n{command}\n); printf '\\n{_RC_MARK}%d\\n' $?"
        try:
            channel.exec_command(wrapped)
            out, err = _drain_channel(channel, timeout)
        finally:
            channel.close()
        match = _RC_RE.search(out)
        code = int(match.group(1)) if match else -1
        body = out[: match.start()] if match else out
        return code, body.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def _sftp(self):
        return self._client.open_sftp()

    def upload(self, local: str | Path, remote: str) -> None:
        """Copy a file onto the machine. Parent dirs must already exist."""
        sftp = self._sftp()
        try:
            sftp.put(str(local), remote)
        finally:
            sftp.close()

    def upload_bytes(self, remote: str, payload: bytes) -> None:
        sftp = self._sftp()
        try:
            with sftp.open(remote, "wb") as handle:
                handle.set_pipelined(True)
                handle.write(payload)
        finally:
            sftp.close()

    def write_text(self, remote: str, text: str) -> None:
        parent = remote.rsplit("/", 1)[0]
        if parent:
            self.run(f"mkdir -p -- {parent}", timeout=10)
        sftp = self._sftp()
        try:
            with sftp.open(remote, "wb") as handle:
                handle.write(text.encode("utf-8"))
        finally:
            sftp.close()

    def read_text(self, remote: str) -> str:
        sftp = self._sftp()
        try:
            with sftp.open(remote, "r") as handle:
                return handle.read().decode("utf-8", "replace")
        finally:
            sftp.close()

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


class SshHttpClient:
    """HTTP over an SSH channel, for a port that is only bound to 127.0.0.1.

    A remote runtime listens for control traffic on the target's loopback so it
    is never exposed on the network. The platform reaches it by opening a
    `direct-tcpip` channel and speaking HTTP through it - no extra listening
    port anywhere, and nothing to firewall.
    """

    def __init__(self, runner: ParamikoRunner, port: int, timeout: float = 8.0) -> None:
        self._runner = runner
        self.port = port
        self.timeout = timeout

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, str]:
        transport = self._runner.client.get_transport()
        if transport is None:
            raise SshError("SSH 连接已断开")
        try:
            channel = transport.open_channel(
                "direct-tcpip", ("127.0.0.1", self.port), ("127.0.0.1", 0), timeout=self.timeout
            )
        except Exception as exc:
            raise SshError(f"无法连到远程运行时控制端口 {self.port}: {exc}") from exc
        try:
            head = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nConnection: close\r\n"
            if body is not None:
                head += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            channel.sendall((head + "\r\n").encode("utf-8") + (body or b""))
            raw = bytearray()
            while True:
                chunk = channel.recv(65536)
                if not chunk:
                    break
                raw.extend(chunk)
        finally:
            channel.close()
        header, _, payload = bytes(raw).partition(b"\r\n\r\n")
        try:
            status = int(header.split()[1])
        except (IndexError, ValueError):
            raise SshError("远程运行时返回了无法解析的响应")
        return status, payload.decode("utf-8", "replace")

    def get_json(self, path: str) -> dict[str, Any]:
        status, text = self.request("GET", path)
        return _json_or_raise(status, text)

    def post_json(self, path: str, payload: Any) -> dict[str, Any]:
        status, text = self.request("POST", path, json.dumps(payload).encode("utf-8"))
        return _json_or_raise(status, text)


def _json_or_raise(status: int, text: str) -> dict[str, Any]:
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {"detail": text[:200]}
    if status >= 400:
        raise SshError(f"远程运行时 {status}: {data.get('detail', text[:200])}")
    return data


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


def listening_ports(runner: SshRunner) -> list[int]:
    """TCP ports currently listening on the far side."""
    _, out, _ = runner.run("ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null || true")
    return _parse_ports(out)


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


def _parse_version(text: str) -> tuple[int, ...]:
    """(3, 11, 2) out of "Python 3.11.2" / "3.6.8"; empty when unreadable."""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        return ()
    return tuple(int(part or 0) for part in match.groups())


def probe_target(runner: SshRunner, deploy_dir: str = DEFAULT_DEPLOY_DIR) -> SiteProbe:
    checked = datetime.now(timezone.utc).isoformat()

    uname = _first_line(runner.run("uname -s -m")[1])
    os_name, _, arch = uname.partition(" ")

    _, os_release, _ = runner.run("cat /etc/os-release 2>/dev/null; true")
    distro = _parse_os_release(os_release)

    _, python_out, _ = runner.run("python3 --version 2>&1; true")
    python_bin = "python3" if "Python 3." in python_out else ""
    python_version = _first_line(python_out) if python_bin else ""

    # Old distros ship `python3` pointing at 3.5/3.6, so a usable interpreter
    # may exist under a versioned name even when `python3` itself is too old.
    python_candidates = _command_paths(
        runner, ("python3.13", "python3.12", "python3.11", "python3.10")
    )
    python_ok = bool(python_bin) and _parse_version(python_version) >= MIN_PYTHON
    if not python_ok and python_candidates:
        python_bin = python_candidates[0]
        _, out, _ = runner.run(f"{python_bin} --version 2>&1; true")
        python_version = _first_line(out)
        python_ok = _parse_version(python_version) >= MIN_PYTHON

    package_manager = ""
    managers = _command_paths(runner, ("apt-get", "dnf", "yum", "zypper"))
    if managers:
        package_manager = Path(managers[0]).name

    _, docker_out, _ = runner.run("docker --version 2>/dev/null; true")
    docker = _first_line(docker_out)
    # Docker being installed says nothing about this SSH user being allowed to
    # drive it; `docker info` is the only honest check.
    docker_ok = False
    if docker:
        code, _, _ = runner.run("docker info >/dev/null 2>&1", timeout=15)
        docker_ok = code == 0

    _, uv_out, _ = runner.run(
        'for p in uv "$HOME/.local/bin/uv"; do command -v "$p" 2>/dev/null; done; true'
    )
    uv = _first_line(uv_out)

    deploy_ok = False
    if _SAFE_PATH.match(deploy_dir or ""):
        code, out, _ = runner.run(f"mkdir -p -- {deploy_dir} && test -w {deploy_dir} && echo OK")
        deploy_ok = "OK" in out

    return SiteProbe(
        ok=True,
        os=os_name or "unknown",
        arch=arch,
        distro=distro,
        python=python_version,
        python_bin=python_bin,
        python_candidates=python_candidates,
        python_ok=python_ok,
        package_manager=package_manager,
        docker=docker,
        docker_ok=docker_ok,
        uv=uv,
        deploy_dir=deploy_dir,
        deploy_ok=deploy_ok,
        ports_in_use=listening_ports(runner),
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
        python_ok=sys.version_info[:2] >= MIN_PYTHON,
        uv=shutil.which("uv") or "",
        deploy_dir=str(data_dir()),
        deploy_ok=True,
        ports_in_use=[],
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
