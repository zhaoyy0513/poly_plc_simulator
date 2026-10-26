"""Port ownership: the one thing two simulator instances can fight over.

Everything else about an instance is per-process already (memory, config file,
log file, control socket), so "is this port mine" is the whole isolation story.
It has two halves and both are tested here:

1. **The pre-flight must answer what the listener will answer.** A probe that
   uses different socket options than the server is a check about a different
   question. On Windows the wrong option (``SO_REUSEADDR``) makes a port that
   somebody is actively listening on look free - the failure mode this module
   exists to prevent.
2. **The listener must refuse a second claimant.** If it does not, two
   instances both report "running" while only one of them receives traffic.
"""

from __future__ import annotations

import socket
import time

import pytest

from poly_contracts import Protocol
from poly_core import can_bind
from poly_modbus import ModbusTcpAdapter

from .util import free_port, make_config


def test_can_bind_accepts_a_free_port() -> None:
    assert can_bind("127.0.0.1", free_port()) is None


def test_can_bind_reports_a_listened_port_as_taken() -> None:
    """The probe must fail on a live listener, including on Windows where a
    bare SO_REUSEADDR bind would succeed."""
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    try:
        problem = can_bind("127.0.0.1", port)
        assert problem is not None, "a listening port was reported as free"
    finally:
        squatter.close()


def test_two_modbus_adapters_cannot_share_a_port() -> None:
    """The second listener must fail loudly instead of silently taking over."""
    first = ModbusTcpAdapter(make_config(Protocol.MODBUS_TCP, []))
    first.start()
    time.sleep(0.2)
    try:
        second = ModbusTcpAdapter(
            make_config(Protocol.MODBUS_TCP, [], port=first.config.port)
        )
        with pytest.raises(OSError):
            second.start()
    finally:
        first.stop()


def test_a_stopped_adapter_releases_its_port() -> None:
    """Restarting an instance on the same port is normal; a stale listener is
    not allowed to keep it."""
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    adapter.stop()

    assert can_bind("127.0.0.1", config.port) is None
    again = ModbusTcpAdapter(make_config(Protocol.MODBUS_TCP, [], port=config.port))
    again.start()
    time.sleep(0.2)
    again.stop()
