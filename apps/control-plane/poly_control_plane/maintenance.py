"""List local poly_sim_runtime processes and kill only test leftovers.

Safety: a process that is in this data dir's manifest (owned) or whose config
is not under a temp/pytest dir is never killed — it may be another control
plane's live business instance.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import psutil


def _owned_identities(data_dir: Path) -> set[tuple[int, float]]:
    owned: set[tuple[int, float]] = set()
    try:
        raw = json.loads((data_dir / "runtimes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return owned
    for record in raw.values():
        if isinstance(record, dict):
            owned.add((int(record.get("pid") or 0), float(record.get("create_time") or 0.0)))
        elif isinstance(record, int):
            owned.add((record, 0.0))
    return owned


def _is_test_config(config: str) -> bool:
    if not config:
        return False
    if "pytest-of" in config:
        return True
    try:
        return Path(config).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):
        return False


def list_runtimes(data_dir: Path) -> list[dict[str, Any]]:
    owned = _owned_identities(data_dir)
    out: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not any("poly_sim_runtime" in part for part in cmdline):
            continue
        # Editor/indexer processes can mention poly_sim_runtime in their
        # command line without being one: a real runtime always carries
        # --config. Require it, or we would offer to kill VS Code.
        config = ""
        control_port = 0
        for i, part in enumerate(cmdline):
            if part == "--config" and i + 1 < len(cmdline):
                config = cmdline[i + 1]
            elif part == "--control-port" and i + 1 < len(cmdline):
                try:
                    control_port = int(cmdline[i + 1])
                except ValueError:
                    pass
        if not config:
            continue
        pid = proc.info["pid"]
        create_time = proc.info["create_time"] or 0.0
        out.append({
            "pid": pid,
            "create_time": create_time,
            "config": config,
            "control_port": control_port,
            "owned": (pid, create_time) in owned or (pid, 0.0) in owned,
            "is_test": _is_test_config(config),
        })
    return out


def kill_test_runtimes(pids: list[int], data_dir: Path) -> list[int]:
    rows = {r["pid"]: r for r in list_runtimes(data_dir)}
    for pid in pids:
        row = rows.get(pid)
        if row is None or row["owned"] or not row["is_test"]:
            raise ValueError(f"pid {pid} 不是可清理的测试残留")
    killed: list[int] = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
            killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed
