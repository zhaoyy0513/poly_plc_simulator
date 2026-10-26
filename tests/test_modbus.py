from __future__ import annotations

import time

import pytest

from poly_contracts import Protocol
from poly_modbus import ModbusTcpAdapter
from pymodbus.client import ModbusTcpClient

from .util import make_config


def test_modbus_client_write_is_visible_in_the_data_area() -> None:
    """Whatever a real client writes must show up in the console, and back."""
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()

        assert client.write_register(10, 4242, device_id=1)
        time.sleep(0.1)
        assert adapter.read_area("holding", 10, 1) == [4242]

        assert client.write_coil(3, True, device_id=1)
        time.sleep(0.1)
        assert adapter.read_area("coil", 3, 1) == [True]
        assert client.write_coil(3, False, device_id=1)
        time.sleep(0.1)
        assert adapter.read_area("coil", 3, 1) == [False]

        # Console side -> client side. A 32 bit value is just two registers
        # here; interpreting them is the client's business, not the server's.
        adapter.write_area("holding", 0, 0x41AC)
        adapter.write_area("holding", 1, 0x0000)
        response = client.read_holding_registers(0, count=2, device_id=1)
        assert response.registers == [0x41AC, 0x0000]
        client.close()
    finally:
        adapter.stop()


def test_modbus_address_space_and_raw_read() -> None:
    """The whole 0..65535 space must work, not just a configured window."""
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        adapter.write_area("holding", 65535, 4321)
        assert adapter.read_area("holding", 65500, 36)[-1] == 4321

        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()
        response = client.read_holding_registers(65500, count=36, device_id=1)
        assert not response.isError()
        assert response.registers[-1] == 4321
        client.close()
    finally:
        adapter.stop()


def test_modbus_raw_area_write_covers_all_four_areas() -> None:
    """The console can preset any area, including the ones read-only on the wire."""
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    adapter.start()
    time.sleep(0.2)
    try:
        adapter.write_area("holding", 7, 1234)
        adapter.write_area("input", 8, 65535)
        adapter.write_area("coil", 9, 1)
        adapter.write_area("discrete", 10, True)

        assert adapter.read_area("holding", 7, 1) == [1234]
        assert adapter.read_area("input", 8, 1) == [65535]
        assert adapter.read_area("coil", 9, 1) == [True]
        assert adapter.read_area("discrete", 10, 1) == [True]

        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()
        assert client.read_holding_registers(7, count=1, device_id=1).registers == [1234]
        assert client.read_input_registers(8, count=1, device_id=1).registers == [65535]
        assert client.read_coils(9, count=1, device_id=1).bits[0] is True
        assert client.read_discrete_inputs(10, count=1, device_id=1).bits[0] is True
        client.close()

        with pytest.raises(ValueError):
            adapter.write_area("holding", 0, 65536)
        with pytest.raises(ValueError):
            adapter.write_area("coil", 0, 2)
    finally:
        adapter.stop()


def test_a_configured_map_size_refuses_addresses_past_it() -> None:
    """`register_count` makes the instance behave like a device with a finite map.

    This is the whole point of the setting: a real PLC answers ILLEGAL_ADDRESS
    (exception code 02) past its configured addresses, and testing that path in
    a WCS client needs a server that refuses the same way.
    """
    config = make_config(Protocol.MODBUS_TCP, [], register_count=100)
    adapter = ModbusTcpAdapter(config)
    assert len(adapter._holding) == 100
    adapter.start()
    time.sleep(0.2)
    try:
        client = ModbusTcpClient("127.0.0.1", port=config.port)
        assert client.connect()

        # Inside the map: normal service, including the last address.
        assert not client.read_holding_registers(90, count=10, device_id=1).isError()
        # Straddling the end is out of range, not a partial read.
        straddle = client.read_holding_registers(90, count=20, device_id=1)
        assert straddle.isError()
        assert straddle.exception_code == 2  # ILLEGAL_ADDRESS
        assert client.read_holding_registers(100, count=1, device_id=1).isError()

        # Writes stop at the same boundary, single and multiple.
        assert not client.write_register(99, 7, device_id=1).isError()
        assert client.write_register(100, 7, device_id=1).isError()
        assert client.write_registers(95, [1] * 10, device_id=1).isError()

        # Bits are sized by the same number.
        assert not client.read_coils(99, count=1, device_id=1).isError()
        assert client.read_coils(99, count=2, device_id=1).isError()
        assert client.write_coil(100, True, device_id=1).isError()

        # The console path refuses too: showing values the wire cannot reach
        # would be a lie about the instance.
        assert adapter.read_area("holding", 99, 1) == [7]
        with pytest.raises(ValueError):
            adapter.read_area("holding", 100, 1)
        with pytest.raises(ValueError):
            adapter.write_area("holding", 100, 1)
        client.close()
    finally:
        adapter.stop()


def test_an_unconfigured_map_still_covers_the_whole_address_space() -> None:
    """No `register_count` = the old behaviour, unchanged: 65536 points."""
    config = make_config(Protocol.MODBUS_TCP, [])
    adapter = ModbusTcpAdapter(config)
    assert len(adapter._holding) == 65536
    assert len(adapter._coils) == 65536
