from __future__ import annotations

import socket

from poly_contracts import ServerConfig, ServerOptions


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def kill_runtimes_under(data_dir) -> None:
    """Terminate poly_sim_runtime processes whose config lives under data_dir."""
    import psutil
    from pathlib import Path

    root = Path(data_dir).resolve()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not any("poly_sim_runtime" in part for part in cmdline):
            continue
        config = next((cmdline[i + 1] for i, p in enumerate(cmdline) if p == "--config" and i + 1 < len(cmdline)), "")
        try:
            if config and Path(config).resolve().is_relative_to(root):
                proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
            continue


def make_config(protocol, tags, port: int | None = None, name: str = "test", **options) -> ServerConfig:
    return ServerConfig(
        id="test-" + protocol.value,
        site_id="site",
        name=name,
        protocol=protocol,
        bind_ip="127.0.0.1",
        port=port or free_port(),
        options=ServerOptions(**options),
        tags=tags,
    )
