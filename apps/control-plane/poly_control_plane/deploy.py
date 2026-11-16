"""Getting the simulator onto another machine, and driving it there.

The model is deliberately boring: we push a source tarball over SSH, build one
virtualenv inside the deployment directory, and start one OS process per
instance. Nothing is installed system-wide and no daemon of ours is left
running, so "uninstalling" a site is `rm -rf ~/.poly-sim`.

Carrier ladder (the machine decides, the UI only reports it):

  native  - the box already has Python >= 3.10, use it
  uv      - Python is too old/absent, so we install a standalone CPython with
            uv. No root, no compiler, ~30 s, everything under $HOME.
  docker  - last resort; only reachable today by machines that have a daemon
            the SSH user may drive. Not implemented yet - see
            docs/plans/2026-10-22-remote-site-server-client-design.md.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import tarfile
import tomllib
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from poly_contracts import (
    DeployPlan,
    RuntimeState,
    ServerConfig,
    SiteProbe,
    config_fingerprint,
)

from .ssh import MIN_PYTHON, SshError, SshHttpClient, SshRunner, listening_ports
from .system import ROOT

TARGET_PYTHON = "3.11"
BUNDLE_TOP = "poly-sim"

# Only what a remote runtime needs. The control plane, the agent and the web
# build stay on the platform box.
SHIPPED_PACKAGES = (
    "packages/contracts",
    "packages/plc-core",
    "packages/proto-modbus",
    "packages/proto-s7",
    "packages/proto-opcua",
)
SHIPPED_APPS = ("apps/sim-runtime",)

# Protocol adapters that pull heavy or platform-specific wheels. The core
# install must never fail because of them, so they go in a second, best-effort
# pass; an S7 instance on a machine without snap7 fails at start with a clear
# ImportError instead of blocking every Modbus instance.
EXTRA_REQUIREMENTS = ("asyncua", "python-snap7")

EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".venv", ".git", "node_modules", "dist", ".mypy_cache"}


class DeployError(RuntimeError):
    """Something on the target machine stopped the deployment."""


# ------------------------------------------------------------------ decision
def decide_carrier(probe: SiteProbe | None) -> DeployPlan:
    """Pick how instances will run on a machine. Never asks the user.

    The result is informational: if a user dislikes it, the fix is on the
    machine (install Python, open the network), not a switch in the UI.
    """
    if probe is None:
        return DeployPlan(
            carrier="unknown",
            label="还没探测过这台机器",
            reason="点「测试连接」，平台会 SSH 上去看能用什么方式部署。",
            action="",
        )
    # Probed, and the machine said no. That is not the same as "not probed
    # yet": the reason is known and worth repeating, because the fix is on
    # the machine and the user is the only one who can make it.
    if not probe.ok:
        windows = probe.os.strip().lower() == "windows"
        return DeployPlan(
            carrier="unsupported",
            # Windows is not a machine to repair: it is a machine that runs the
            # control plane itself. Say the next step, not the defect.
            label="Windows 机器只能本机运行" if windows else "这台机器不能部署实例",
            reason=probe.error or "探测失败，原因未知。",
            action=(
                "在那台 Windows 上直接运行控制面，host 留空即可；远程实例请用 Linux。"
                if windows
                else "按上面的提示处理这台机器，再点一次「测试连接」。"
            ),
        )
    version = _bare_version(probe.python)
    if probe.python_ok:
        return DeployPlan(
            carrier="native",
            label=f"使用 Python {version}",
            reason=(
                f"{probe.distro or probe.os} 上已有 Python ≥ {_fmt_min()}"
                f"（{probe.python_bin or probe.python}），平台直接用它建虚拟环境。"
            ),
            action="建虚拟环境 → 上传源码 → 装依赖 → 起进程",
            python=probe.python_bin,
            ready=True,
        )
    if probe.docker_ok:
        return DeployPlan(
            carrier="docker",
            label="Docker 部署",
            reason=f"没有 Python ≥ {_fmt_min()}（当前 {version or '未检测到'}），但 Docker({probe.docker}) 可用。",
            action="（Docker 驱动尚未实现，请先安装 Python 3.10+ 或开放网络）",
            ready=False,
        )
    return DeployPlan(
        carrier="uv",
        label=f"自动安装 Python {TARGET_PYTHON}",
        reason=(
            f"{probe.distro or probe.os} 自带 Python {version or '未检测到'}，低于 {_fmt_min()}。"
            f"平台会用 uv 装一个独立 Python {TARGET_PYTHON}（约 30 秒，只写部署目录和 ~/.local，不碰系统 Python）。"
        ),
        action="装 uv → 装 Python → 建虚拟环境 → 上传源码 → 装依赖 → 起进程",
        python="",
        ready=True,
    )


def _bare_version(text: str) -> str:
    """'Python 3.6.8' -> '3.6.8'."""
    match = re.search(r"\d+(?:\.\d+)*", text or "")
    return match.group(0) if match else ""


def _fmt_min() -> str:
    return ".".join(str(part) for part in MIN_PYTHON)


# ------------------------------------------------------------------ bundle
def _excluded(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    return relative.endswith((".pyc", ".pyo", ".egg-info"))


def collect_requirements(root: Path) -> tuple[list[str], list[str]]:
    """Third-party requirements of the shipped packages, split core / extra.

    Local `poly-*` dependencies are dropped: they are installed from the
    tarball in the same command, and pip would fail trying to resolve them
    from an index that has never heard of us.
    """
    core: list[str] = []
    extra: list[str] = []
    for relative in (*SHIPPED_PACKAGES, *SHIPPED_APPS):
        project = tomllib.loads((root / relative / "pyproject.toml").read_text(encoding="utf-8"))
        for dependency in project.get("project", {}).get("dependencies", []):
            name = re.split(r"[<>=!;\[ ]", dependency, maxsplit=1)[0].strip()
            if name.startswith("poly-") or dependency in core or dependency in extra:
                continue
            if name in EXTRA_REQUIREMENTS:
                extra.append(dependency)
            else:
                core.append(dependency)
    return core, extra


def build_bundle(root: Path = ROOT) -> tuple[bytes, str]:
    """Tar up the runtime source, with a content hash as its version.

    The hash is what makes every later step idempotent: same hash means the
    machine already has these bits and nothing is uploaded or reinstalled.
    """
    files: list[tuple[str, Path]] = []
    for relative in (*SHIPPED_PACKAGES, *SHIPPED_APPS):
        base = root / relative
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_dir():
                continue
            key = path.relative_to(root).as_posix()
            if _excluded(key):
                continue
            files.append((key, path))

    core, extra = collect_requirements(root)
    requirements = "\n".join(core) + "\n"
    extra_requirements = "\n".join(extra) + "\n"

    digest = hashlib.sha256()
    digest.update(requirements.encode())
    digest.update(extra_requirements.encode())
    for key, path in files:
        digest.update(key.encode())
        digest.update(path.read_bytes())
    version = digest.hexdigest()[:16]

    buffer = BytesIO()
    # The gzip header carries a timestamp by default. Two builds of the same
    # tree would then differ across a second boundary even though the version
    # hash is identical, defeating the "same hash means no upload" contract.
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name, text in (
                ("requirements.txt", requirements),
                ("requirements-extra.txt", extra_requirements),
                ("VERSION", version + "\n"),
            ):
                payload = text.encode()
                info = tarfile.TarInfo(f"{BUNDLE_TOP}/{name}")
                info.size = len(payload)
                info.mtime = 0
                tar.addfile(info, BytesIO(payload))
            for key, path in files:
                info = tar.gettarinfo(str(path), arcname=f"{BUNDLE_TOP}/{key}")
                info.mtime = 0
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
    return buffer.getvalue(), version


# ------------------------------------------------------------------ host
@dataclass
class RemoteHost:
    """Everything we do on one target machine."""

    runner: SshRunner
    deploy_dir: str = "~/.poly-sim"
    # Absolute deployment directory once known; `~` or empty before that.
    root: str = ""
    log: list[str] = field(default_factory=list)

    # -- paths ------------------------------------------------------------
    def resolve(self) -> str:
        """Absolute deployment directory.

        `~` only means something to a shell: SFTP takes paths literally, so
        every file operation needs the real path.
        """
        if not self.root.startswith("/"):
            out = self.run(f"cd {self.deploy_dir} 2>/dev/null && pwd; true", timeout=15)
            candidate = out.splitlines()[-1].strip() if out else ""
            self.root = candidate if candidate.startswith("/") else self.deploy_dir
        return self.root

    @property
    def app_dir(self) -> str:
        return f"{self.resolve()}/app"

    @property
    def venv_python(self) -> str:
        return f"{self.resolve()}/venv/bin/python"

    def instance_dir(self, server_id: str) -> str:
        return f"{self.resolve()}/instances/{server_id}"

    # -- shell ------------------------------------------------------------
    def run(self, command: str, timeout: float = 60.0, check: bool = False) -> str:
        code, out, err = self.runner.run(command, timeout=timeout)
        self.log.append(f"$ {command[:160]} -> {code}")
        if check and code != 0:
            raise DeployError(_tail(err) or _tail(out) or f"命令退出码 {code}")
        return out.strip()

    def _installed_version(self) -> str:
        _, out, _ = self.runner.run(f"cat {self.resolve()}/.version 2>/dev/null; true")
        return out.strip()

    # -- provisioning ------------------------------------------------------
    def ensure_ready(self, probe: SiteProbe | None) -> tuple[str, list[str]]:
        """Make the machine able to run instances. Returns (python, steps)."""
        plan = decide_carrier(probe)
        if plan.carrier == "unknown":
            raise DeployError("还没有探测过这台机器，先点「测试连接」")
        if plan.carrier == "unsupported":
            raise DeployError(plan.reason)
        if plan.carrier == "docker":
            raise DeployError("Docker 部署驱动还没实现；请在目标机安装 Python 3.10+（或开放网络让平台装 uv）")

        self.run(f"mkdir -p -- {self.deploy_dir}", check=True)

        if plan.carrier == "uv":
            python = self._ensure_uv_python()
        else:
            python = plan.python or (probe.python_bin if probe else "python3")
        self._ensure_venv(python)
        self._sync_source()
        self._ensure_deps()
        return python, list(self.log)

    def _ensure_uv_python(self) -> str:
        uv = self.run(
            'for p in uv "$HOME/.local/bin/uv"; do command -v "$p" 2>/dev/null; done; true'
        ).splitlines()
        uv_bin = uv[0].strip() if uv else ""
        if not uv_bin:
            self.run(
                "curl -LsSf https://astral.sh/uv/install.sh | sh",
                timeout=180,
                check=True,
            )
            uv_bin = self.run(
                'command -v "$HOME/.local/bin/uv" || command -v uv; true'
            ).splitlines()[0].strip()
        if not uv_bin:
            raise DeployError("装不上 uv：目标机可能访问不到 astral.sh，请手工安装 Python 3.10+")
        self.run(f'"{uv_bin}" python install {TARGET_PYTHON}', timeout=300, check=True)
        found = self.run(f'"{uv_bin}" python find {TARGET_PYTHON}', timeout=60, check=True)
        return found.splitlines()[-1].strip()

    def _ensure_venv(self, python: str) -> None:
        _, out, _ = self.runner.run(f'test -x {self.venv_python} && echo OK; true')
        if "OK" in out:
            return
        self.run(f'"{python}" -m venv {self.resolve()}/venv', timeout=300, check=True)

    def _sync_source(self) -> None:
        payload, version = build_bundle()
        if self._installed_version() == version:
            return
        remote_tar = f"{self.resolve()}/poly-sim.tar.gz"
        self.runner.upload_bytes(remote_tar, payload)
        self.run(
            f"rm -rf {self.resolve()}/app.new && mkdir -p {self.resolve()}/app.new && "
            f"tar -xzf {remote_tar} -C {self.resolve()}/app.new --strip-components=1 && "
            f"rm -rf {self.app_dir} && mv {self.resolve()}/app.new {self.app_dir} && "
            f"rm -f {remote_tar}",
            timeout=180,
            check=True,
        )
        self.runner.write_text(f"{self.resolve()}/.version", version + "\n")

    def _ensure_deps(self) -> None:
        version = self._installed_version()
        marker = f"{self.resolve()}/.installed"
        _, out, _ = self.runner.run(f"cat {marker} 2>/dev/null; true")
        if out.strip() == version and version:
            return
        pip = f"{self.venv_python} -m pip install -q --disable-pip-version-check"
        # --no-deps: the poly-* packages are all in the tarball, and pip would
        # otherwise try to resolve them from PyPI where they do not exist.
        # --force-reinstall: every build carries the same 0.1.0 version, so pip
        # would otherwise decide the new code is "already satisfied" and leave
        # the machine running last week's runtime.
        self.run(
            f"cd {self.app_dir} && {pip} --no-deps --force-reinstall ./packages/contracts "
            f"./packages/plc-core ./packages/proto-modbus {self._app_ref()}",
            timeout=600,
            check=True,
        )
        self.run(f"cd {self.app_dir} && {pip} -r requirements.txt", timeout=600, check=True)
        # Protocol adapters are optional: never let snap7/asyncua break Modbus.
        self.run(
            f"cd {self.app_dir} && {pip} --no-deps --force-reinstall ./packages/proto-s7 "
            f"./packages/proto-opcua && {pip} -r requirements-extra.txt || true",
            timeout=600,
        )
        self.runner.write_text(marker, version + "\n")

    def _app_ref(self) -> str:
        return "./apps/sim-runtime"

    # -- instances ---------------------------------------------------------
    def start_instance(self, config: ServerConfig, control_port: int) -> str:
        """Make sure one runtime is running; return "adopted" or "started".

        A remote runtime is a background process on the target, so it outlives
        the control plane. After a restart the old code path killed it and
        spawned a fresh one, which silently reset every data area. Adoption is
        the same idea as the local supervisor: if the pid is alive and its
        `/state` reports the same instance and config fingerprint, keep the
        process. Only an unidentifiable or stale runtime is replaced.
        """
        directory = self.instance_dir(config.id)
        self.run(f"mkdir -p -- {directory}", check=True)
        if self._adopt_instance(config, control_port):
            return "adopted"
        self.runner.write_text(
            f"{directory}/config.json", config.model_dump_json(indent=2)
        )
        self.runner.write_text(f"{directory}/control.port", f"{control_port}\n")
        log = f"{directory}/runtime.log"
        pid = f"{directory}/runtime.pid"
        self._kill(config.id)
        # `exec` inside the backgrounded subshell is what makes `$!` the real
        # python pid; without it the pid belongs to a shell that outlives the
        # kill and leaves the runtime orphaned holding the port.
        self.run(
            f"cd {self.resolve()} && "
            f"( PYTHONUNBUFFERED=1 exec {self.venv_python} -m poly_sim_runtime "
            f"--config {directory}/config.json --control-port {control_port} --host 127.0.0.1 "
            f") >> {log} 2>&1 < /dev/null & echo $! > {pid}; "
            f"sleep 1; cat {pid}",
            timeout=45,
            check=True,
        )
        return "started"

    def _adopt_instance(self, config: ServerConfig, control_port: int) -> bool:
        """Can the runtime already on the target be kept as-is?"""
        if not control_port:
            return False
        directory = self.instance_dir(config.id)
        pid_path = f"{directory}/runtime.pid"
        _, out, _ = self.runner.run(
            f'if [ -f {pid_path} ] && kill -0 "$(cat {pid_path})" 2>/dev/null; '
            f"then cat {pid_path}; fi; true"
        )
        try:
            pid = int(out.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return False
        try:
            state = self.fetch_state(config.id, control_port)
        except Exception:
            # Alive but unreachable: hung, still starting, or the port belongs
            # to something else. Let the caller replace it.
            return False
        if state.server_id != config.id:
            return False
        if state.config_hash != config_fingerprint(config):
            # Config changed while the control plane was away, or this is an
            # older runtime that does not report a fingerprint.
            return False
        if state.pid and state.pid != pid:
            return False
        return state.status in ("running", "starting")

    def _kill(self, server_id: str) -> None:
        """Stop this instance's runtime, pid file or not.

        The config path is unique per instance, so matching on it is safe and
        still works after a reboot or a lost pid file - exactly the cases where
        a stale process would otherwise keep the port. Deliberately no
        `kill "$(cat pid)"`: after a reboot that pid can belong to an unrelated
        process, while `pkill -f` matches only our runtime command line.
        """
        directory = self.instance_dir(server_id)
        pid = f"{directory}/runtime.pid"
        self.run(
            f'pkill -f "poly_sim_runtime --config {directory}/config.json" 2>/dev/null; '
            f"sleep 0.6; "
            f'pkill -9 -f "poly_sim_runtime --config {directory}/config.json" 2>/dev/null; '
            f"rm -f {pid}; true",
            timeout=30,
        )

    def stop_instance(self, server_id: str) -> None:
        self._kill(server_id)

    def remove_instance(self, server_id: str) -> None:
        self.stop_instance(server_id)
        self.run(f"rm -rf -- {self.instance_dir(server_id)}", timeout=30)

    def is_alive(self, server_id: str) -> bool:
        pid = f"{self.instance_dir(server_id)}/runtime.pid"
        _, out, _ = self.runner.run(
            f'if [ -f {pid} ] && kill -0 "$(cat {pid})" 2>/dev/null; then echo UP; fi; true'
        )
        return "UP" in out

    def control_port_of(self, server_id: str) -> int:
        try:
            text = self.runner.read_text(f"{self.instance_dir(server_id)}/control.port")
        except Exception:
            return 0
        return int(text.strip() or 0)

    def http(self, control_port: int) -> SshHttpClient:
        return SshHttpClient(self.runner, control_port)  # type: ignore[arg-type]

    def fetch_state(self, server_id: str, control_port: int) -> RuntimeState:
        data = self.http(control_port).get_json("/state")
        return RuntimeState(**data)

    def logs(self, server_id: str, lines: int = 200) -> str:
        log = f"{self.instance_dir(server_id)}/runtime.log"
        code, out, _ = self.runner.run(f"tail -n {int(lines)} {log} 2>/dev/null; true")
        return out if code == 0 else ""


def _tail(text: str, limit: int = 600) -> str:
    """Last lines of a command's output - the part that says what went wrong."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(lines[-12:])[:limit]


# ------------------------------------------------------------------ supervisor
class RemoteSupervisor:
    """Mirror of `poly_agent.Supervisor` for instances on another machine.

    Same method surface, so the control plane does not care whether an
    instance is a local process or one on a box across the room.

    It holds a *factory*, not a connection: SSH links drop for all the usual
    reasons and the pool reopens them, so every operation asks for a live one.
    """

    def __init__(self, runner_factory: Callable[[], SshRunner], deploy_dir: str) -> None:
        self._factory = runner_factory
        self._deploy_dir = deploy_dir
        self._root = ""
        self._configs: dict[str, ServerConfig] = {}
        self._control_ports: dict[str, int] = {}

    @property
    def host(self) -> RemoteHost:
        host = RemoteHost(self._factory(), self._deploy_dir, root=self._root)
        # Resolving costs one round trip; do it once per site, not per call.
        self._root = host.resolve()
        return host

    # -- lifecycle ---------------------------------------------------------
    def start(self, config: ServerConfig, probe: SiteProbe | None) -> RuntimeState:
        self._configs[config.id] = config
        control_port = config.control_port or self.host.control_port_of(config.id)
        self._control_ports[config.id] = control_port
        self.host.ensure_ready(probe)
        self.host.start_instance(config, control_port)
        return self.state(config.id)

    def stop(self, server_id: str) -> None:
        self.host.stop_instance(server_id)
        self._configs.pop(server_id, None)

    def restart(self, config: ServerConfig, probe: SiteProbe | None) -> RuntimeState:
        self.stop(config.id)
        return self.start(config, probe)

    def forget(self, server_id: str) -> None:
        self.host.remove_instance(server_id)
        self._configs.pop(server_id, None)

    # -- observation -------------------------------------------------------
    def state(self, server_id: str) -> RuntimeState:
        control_port = self._control_ports.get(server_id) or self.host.control_port_of(server_id)
        if not control_port or not self.host.is_alive(server_id):
            return RuntimeState(server_id=server_id, status="stopped")
        try:
            data = self.host.http(control_port).get_json("/state")
        except SshError as exc:
            return RuntimeState(server_id=server_id, status="error", error=str(exc))
        if not data:
            return RuntimeState(server_id=server_id, status="starting")
        return RuntimeState(**data)

    def values(self, server_id: str) -> RuntimeState:
        return self.state(server_id)

    def write(self, server_id: str, tag: str, value: Any) -> None:
        self._post(server_id, "/write", {"tag": tag, "value": value})

    def read_area(self, server_id: str, area: str, address: int, count: int) -> list[Any]:
        data = self._get(
            server_id, f"/area?area={area}&address={address}&count={count}"
        )
        return list(data.get("values", []))

    def write_area(self, server_id: str, area: str, address: int, value: Any) -> None:
        self._post(server_id, "/area/write", {"area": area, "address": address, "value": value})

    def connections(self, server_id: str) -> list[dict[str, Any]]:
        data = self._get(server_id, "/connections")
        return list(data.get("connections", []))

    def kick_connection(self, server_id: str, conn_id: str) -> None:
        self._post(server_id, f"/connections/{quote(conn_id, safe='')}/kick", {})

    def logs(self, server_id: str, lines: int = 200) -> str:
        return self.host.logs(server_id, lines)

    def port_in_use(self, port: int) -> bool:
        """Live answer from the far side, not the cached probe's word for it.

        A site probe can be minutes old - it is taken for the deploy dialog,
        not on every write - so a port freed or taken since then answers with
        a lie. One `ss` round trip is the truth, and it is the same round trip
        `probe_target` already makes.
        """
        return port in listening_ports(self._factory())

    # -- internals ---------------------------------------------------------
    def _client(self, server_id: str) -> SshHttpClient:
        control_port = self._control_ports.get(server_id) or self.host.control_port_of(server_id)
        if not control_port:
            raise SshError("实例还没有分配控制端口")
        if not self.host.is_alive(server_id):
            raise SshError("实例未在运行")
        return self.host.http(control_port)

    def _get(self, server_id: str, path: str) -> dict[str, Any]:
        return self._client(server_id).get_json(path)

    def _post(self, server_id: str, path: str, payload: Any) -> dict[str, Any]:
        return self._client(server_id).post_json(path, payload)
