"""Deployment decisions and the source bundle.

Everything here is pure logic: no SSH, no target machine. The parts that talk
to a real box are exercised by hand against a live site.
"""

from __future__ import annotations

import io
import tarfile

from poly_contracts import (
    Protocol,
    RuntimeState,
    ServerConfig,
    ServerOptions,
    SiteProbe,
    config_fingerprint,
)
from poly_control_plane.deploy import (
    RemoteHost,
    build_bundle,
    collect_requirements,
    decide_carrier,
)
from poly_control_plane.ssh import probe_target
from poly_control_plane.system import ROOT


def probe(**overrides) -> SiteProbe:
    base = dict(ok=True, os="Linux", distro="Ubuntu 22.04.3 LTS")
    base.update(overrides)
    return SiteProbe(**base)


class FakeRunner:
    """Records the commands and answers them from a table.

    Default answer is "command not found" - which is exactly what an SSH
    session into Windows says for every POSIX tool we rely on.
    """

    def __init__(self, answers: dict[str, tuple[int, str, str]]) -> None:
        self.answers = answers
        self.commands: list[str] = []

    def run(self, command: str, timeout: float = 10.0) -> tuple[int, str, str]:
        self.commands.append(command)
        return self.answers.get(command, (1, "", "not found"))

    def close(self) -> None:
        pass


def test_carrier_native_when_python_is_new_enough() -> None:
    plan = decide_carrier(
        probe(python="Python 3.11.2", python_bin="/usr/bin/python3.11", python_ok=True)
    )
    assert plan.carrier == "native"
    assert plan.ready is True
    assert plan.python == "/usr/bin/python3.11"


def test_carrier_uv_when_python_is_too_old() -> None:
    # The real case this was written for: CentOS 8 ships Python 3.6.8.
    plan = decide_carrier(
        probe(distro="CentOS Linux 8", python="Python 3.6.8", python_bin="python3", python_ok=False)
    )
    assert plan.carrier == "uv"
    assert plan.ready is True
    assert "3.6.8" in plan.reason


def test_carrier_docker_is_not_ready() -> None:
    plan = decide_carrier(
        probe(python="Python 3.6.8", python_ok=False, docker="Docker version 20.10.7", docker_ok=True)
    )
    assert plan.carrier == "docker"
    # Reported as an option, but we refuse to pretend it works.
    assert plan.ready is False


def test_carrier_unknown_before_first_probe() -> None:
    plan = decide_carrier(None)
    assert plan.carrier == "unknown"
    assert plan.ready is False


def test_a_refused_probe_is_unsupported_not_unknown() -> None:
    # "not probed yet" and "probed and refused" used to share one carrier, so
    # a Windows target showed "还没探测过这台机器" and the deploy failed later
    # with a shell error nobody could act on.
    plan = decide_carrier(probe(ok=False, error="Windows 不做远程部署：请在那台 Windows 上直接运行控制面"))
    assert plan.carrier == "unsupported"
    assert plan.ready is False
    assert "不做远程部署" in plan.reason


def test_windows_probe_offers_the_local_route_not_a_repair() -> None:
    # Windows is out of scope for remote deploys by decision, not broken, so the
    # plan must name the next step ("run the control plane there") instead of
    # telling the user to fix something.
    plan = decide_carrier(probe(ok=False, os="Windows", error="Windows 不做远程部署：…"))
    assert plan.carrier == "unsupported"
    assert plan.ready is False
    assert "本机运行" in plan.label
    assert "host 留空" in plan.action


def test_probe_accepts_a_linux_target() -> None:
    runner = FakeRunner({"uname -s": (0, "Linux\n", ""), "uname -s -m": (0, "Linux x86_64\n", "")})
    result = probe_target(runner)
    assert result.ok is True
    assert result.os == "Linux"
    assert result.arch == "x86_64"


def test_probe_refuses_a_windows_target_without_touching_posix_tools() -> None:
    runner = FakeRunner(
        {
            "uname -s": (1, "", "'uname' is not recognized as an internal or external command"),
            "cmd /c ver 2>/dev/null; true": (0, "Microsoft Windows [Version 10.0.19045.3693]", ""),
        }
    )
    result = probe_target(runner)
    assert result.ok is False
    # `os` stays set so the UI can word this as a rule ("Windows 请本机运行")
    # rather than as a failed connection.
    assert result.os == "Windows"
    assert result.error and "Windows 不做远程部署" in result.error
    assert "host 留空" in result.error
    # Bail after the verdict. Every later probe command would fail in its own
    # Windows-specific way, and the pile of failures reads as a broken machine
    # rather than as an unsupported one.
    assert runner.commands == ["uname -s", "cmd /c ver 2>/dev/null; true"]


def test_probe_refuses_a_non_linux_posix_target() -> None:
    # macOS answers `uname`, so it would sail past the Windows check and then
    # fail on `ss` / `pkill` in ways that look like a broken machine.
    runner = FakeRunner({"uname -s": (0, "Darwin\n", "")})
    result = probe_target(runner)
    assert result.ok is False
    assert result.os == "Darwin"
    assert result.error and "只在 Linux 上支持" in result.error
    assert runner.commands == ["uname -s"]


def test_probe_refuses_git_bash_on_windows_too() -> None:
    # Git-Bash answers `uname`, so the "no uname at all" check alone would let
    # it through - and then `ss -ltn` and `pkill -f` would silently find
    # nothing, which is worse than refusing.
    runner = FakeRunner({"uname -s": (0, "MINGW64_NT-10.0-22631\n", "")})
    result = probe_target(runner)
    assert result.ok is False
    assert result.os == "Windows"
    assert result.error and "Windows 不做远程部署" in result.error


def test_requirements_drop_local_packages() -> None:
    core, extra = collect_requirements(ROOT)
    assert not [item for item in core + extra if item.startswith("poly-")]
    assert any(item.startswith("fastapi") for item in core)
    # snap7 / asyncua are the ones allowed to fail without taking Modbus down.
    assert any(item.startswith("python-snap7") for item in extra)
    assert any(item.startswith("asyncua") for item in extra)


def test_bundle_is_stable_and_carries_requirements() -> None:
    first, version_a = build_bundle(ROOT)
    second, version_b = build_bundle(ROOT)
    # Content-addressed: two builds of the same tree must be byte identical,
    # otherwise every instance start would re-upload and reinstall.
    assert version_a == version_b
    assert first == second

    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "poly-sim/requirements.txt" in names
    assert "poly-sim/requirements-extra.txt" in names
    assert "poly-sim/packages/contracts/poly_contracts/models.py" in names
    assert "poly-sim/apps/sim-runtime/poly_sim_runtime/__main__.py" in names
    # The control plane never goes to a target machine.
    assert not any("poly_control_plane" in name for name in names)
    assert not any("__pycache__" in name for name in names)


# ---------------------------------------------------------------- remote adopt
def remote_config(port: int = 15020) -> ServerConfig:
    return ServerConfig(
        id="remote-modbus",
        site_id="site",
        name="remote",
        protocol=Protocol.MODBUS_TCP,
        bind_ip="0.0.0.0",
        port=port,
        options=ServerOptions(),
        tags=[],
    )


def pid_check_command(server_id: str) -> str:
    directory = f"/home/test/.poly-sim/instances/{server_id}"
    return (
        f'if [ -f {directory}/runtime.pid ] && kill -0 "$(cat {directory}/runtime.pid)" 2>/dev/null; '
        f"then cat {directory}/runtime.pid; fi; true"
    )


def test_remote_start_instance_adopts_matching_runtime(monkeypatch) -> None:
    cfg = remote_config()
    directory = f"/home/test/.poly-sim/instances/{cfg.id}"
    runner = FakeRunner(
        {
            f"mkdir -p -- {directory}": (0, "", ""),
            pid_check_command(cfg.id): (0, "123\n", ""),
        }
    )
    host = RemoteHost(runner, "~/.poly-sim", root="/home/test/.poly-sim")
    state = RuntimeState(
        server_id=cfg.id,
        status="running",
        config_hash=config_fingerprint(cfg),
        pid=123,
    )
    monkeypatch.setattr(host, "fetch_state", lambda server_id, port: state)

    assert host.start_instance(cfg, 25000) == "adopted"
    # The old code killed first and asked questions later; adoption must not
    # touch the running process or overwrite its config.
    assert not any("pkill" in command for command in runner.commands)


def test_remote_adoption_rejects_changed_config_or_dead_state(monkeypatch) -> None:
    cfg = remote_config()
    runner = FakeRunner({pid_check_command(cfg.id): (0, "123\n", "")})
    host = RemoteHost(runner, "~/.poly-sim", root="/home/test/.poly-sim")
    good = RuntimeState(
        server_id=cfg.id,
        status="running",
        config_hash=config_fingerprint(cfg),
        pid=123,
    )
    monkeypatch.setattr(host, "fetch_state", lambda server_id, port: good)
    assert host._adopt_instance(cfg, 25000) is True

    # Same process, different desired config: it is obsolete.
    changed = cfg.model_copy(update={"port": 15021})
    assert host._adopt_instance(changed, 25000) is False

    # Same config, but the runtime says it is in error: restart instead of
    # presenting a corpse as healthy.
    bad = good.model_copy(update={"status": "error"})
    monkeypatch.setattr(host, "fetch_state", lambda server_id, port: bad)
    assert host._adopt_instance(cfg, 25000) is False

    # The control port answers for a different pid than the pid file: not ours.
    other = good.model_copy(update={"pid": 456})
    monkeypatch.setattr(host, "fetch_state", lambda server_id, port: other)
    assert host._adopt_instance(cfg, 25000) is False
