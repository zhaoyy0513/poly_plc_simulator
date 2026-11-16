"""Modbus client-connection tracking and kick."""

from __future__ import annotations

import time

import pytest
from pymodbus.client import ModbusTcpClient

from poly_contracts import Protocol
from poly_core import ProtocolAdapter
from poly_modbus import ModbusTcpAdapter

from .util import make_config


def test_adapter_base_has_empty_connections_by_default() -> None:
    class Dummy(ProtocolAdapter):
        protocol = "dummy"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    adapter = Dummy(make_config(Protocol.MODBUS_TCP, []))
    assert adapter.connections() == []
    with pytest.raises(NotImplementedError):
        adapter.kick("nope")


def test_modbus_connections_are_tracked_and_can_be_kicked() -> None:
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        assert adapter.connections() == []

        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()
        # connect() only opens the socket; a real request is what the record counts.
        assert not client.read_holding_registers(0, count=1, device_id=1).isError()
        time.sleep(0.2)

        connections = adapter.connections()
        assert len(connections) == 1
        conn = connections[0]
        assert conn["remote"].startswith("127.0.0.1:")
        assert conn["requests"] >= 1
        assert conn["unit_id"] == 1
        assert "conn" not in conn
        conn_id = conn["id"]

        adapter.kick(conn_id)
        with pytest.raises(KeyError):
            adapter.kick(conn_id)
        time.sleep(0.3)
        assert adapter.connections() == []
        assert adapter.clients == 0

        client.close()
    finally:
        adapter.stop()
