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
from poly_control_plane.system import ROOT


def probe(**overrides) -> SiteProbe:
    base = dict(ok=True, os="Linux", distro="Ubuntu 22.04.3 LTS")
    base.update(overrides)
    return SiteProbe(**base)


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
