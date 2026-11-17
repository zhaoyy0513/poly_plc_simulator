"""Reset clears the data plane without dropping clients."""

from __future__ import annotations

import time

from pymodbus.client import ModbusTcpClient

from poly_contracts import Protocol
from poly_modbus import ModbusTcpAdapter

from .util import make_config


def test_modbus_reset_zeroes_areas_and_keeps_clients() -> None:
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()
        assert client.write_register(5, 1234, device_id=1)
        assert client.write_coil(2, True, device_id=1)
        time.sleep(0.1)
        assert adapter.read_area("holding", 5, 1) == [1234]

        adapter.reset()

        assert adapter.read_area("holding", 5, 1) == [0]
        assert adapter.read_area("coil", 2, 1) == [False]
        # connection survived the reset
        assert adapter.clients == 1
        assert not client.read_holding_registers(5, count=1, device_id=1).isError()
        client.close()
    finally:
        adapter.stop()