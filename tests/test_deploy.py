"""Deployment decisions and the source bundle.

Everything here is pure logic: no SSH, no target machine. The parts that talk
to a real box are exercised by hand against a live site.
"""

from __future__ import annotations

import io
import tarfile

from poly_contracts import SiteProbe
from poly_control_plane.deploy import (
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
    plan = decide_carrier(probe(ok=False, error="目标机不是 Linux：远程部署要靠 POSIX 工具"))
    assert plan.carrier == "unsupported"
    assert plan.ready is False
    assert "不是 Linux" in plan.reason


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
    assert result.os == "Windows"
    assert result.error and "Linux" in result.error
    # Bail after the verdict. Every later probe command would fail in its own
    # Windows-specific way, and the pile of failures reads as a broken machine
    # rather than as an unsupported one.
    assert runner.commands == ["uname -s", "cmd /c ver 2>/dev/null; true"]


def test_probe_refuses_git_bash_on_windows_too() -> None:
    # Git-Bash answers `uname`, so the "no uname at all" check alone would let
    # it through - and then `ss -ltn` and `pkill -f` would silently find
    # nothing, which is worse than refusing.
    runner = FakeRunner({"uname -s": (0, "MINGW64_NT-10.0-22631\n", "")})
    result = probe_target(runner)
    assert result.ok is False
    assert result.error and "Linux" in result.error


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
