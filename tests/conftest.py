from __future__ import annotations

import socket

from poly_contracts import DataType, ServerConfig, ServerOptions, TagDef, WordOrder
from poly_core import from_registers, to_registers


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def config(protocol, tags, port=None, name="test", **options) -> ServerConfig:
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


def test_float32_word_order() -> None:
    big = to_registers(1.5, DataType.FLOAT32, WordOrder.BIG)
    little = to_registers(1.5, DataType.FLOAT32, WordOrder.LITTLE)
    assert big != little
    assert from_registers(big, DataType.FLOAT32, WordOrder.BIG) == 1.5
    assert from_registers(little, DataType.FLOAT32, WordOrder.LITTLE) == 1.5
