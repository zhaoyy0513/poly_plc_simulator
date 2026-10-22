from __future__ import annotations

import socket

from poly_contracts import ServerConfig, ServerOptions


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


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
