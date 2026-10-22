from __future__ import annotations

import os
import socket
import sys
import platform
from pathlib import Path

from poly_contracts import SystemInfo

ROOT = Path(__file__).resolve().parents[3]
DIST = ROOT / "apps" / "web" / "dist"


def data_dir() -> Path:
    configured = os.environ.get("POLY_DATA_DIR")
    path = Path(configured) if configured else ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def detect_lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        try:
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return "127.0.0.1"


def local_addresses() -> list[str]:
    addresses = {"127.0.0.1", detect_lan_ip()}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    return sorted(address for address in addresses if address)


def probe_protocols() -> tuple[list[str], dict[str, str]]:
    available: list[str] = []
    notes: dict[str, str] = {}
    try:
        import poly_modbus  # noqa: F401

        available.append("modbus_tcp")
    except Exception as exc:  # pragma: no cover - environment dependent
        notes["modbus_tcp"] = f"unavailable: {exc}"
    try:
        import poly_s7  # noqa: F401

        available.append("s7")
        notes["s7"] = "python-snap7 3.x pure-python server"
    except Exception as exc:  # pragma: no cover
        notes["s7"] = f"unavailable: {exc}"
    try:
        import poly_opcua  # noqa: F401

        available.append("opcua")
    except Exception as exc:  # pragma: no cover
        notes["opcua"] = f"unavailable: {exc}"
    return available, notes


def system_info() -> SystemInfo:
    protocols, notes = probe_protocols()
    return SystemInfo(
        os=platform.platform(),
        arch=platform.machine(),
        python=sys.version.split()[0],
        hostname=socket.gethostname(),
        addresses=local_addresses(),
        protocols=protocols,
        protocol_notes=notes,
    )
