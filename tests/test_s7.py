from __future__ import annotations

import time

import snap7
from poly_contracts import DataType, Protocol, TagDef
from poly_s7 import S7Adapter

from .util import make_config


def test_s7_db_roundtrip() -> None:
    tags = [
        TagDef(name="temp", data_type=DataType.FLOAT32, area="db", db_number=1, address=0),
        TagDef(name="run", data_type=DataType.BOOL, area="m", address=0, bit=2),
    ]
    config = make_config(Protocol.S7, tags)
    adapter = S7Adapter(config)
    adapter.start()
    time.sleep(0.3)
    client = snap7.client.Client()
    try:
        client.connect("127.0.0.1", 0, 0, config.port)
        assert client.get_connected()

        adapter.write_tag(tags[0], 12.25)
        time.sleep(0.1)
        raw = client.db_read(1, 0, 4)
        assert bytes(raw) == b"\x41\x44\x00\x00"

        client.db_write(1, 0, bytearray(b"\x42\x48\x00\x00"))
        time.sleep(0.1)
        assert abs(adapter.read_tag(tags[0]) - 50.0) < 0.001

        adapter.write_tag(tags[1], True)
        time.sleep(0.1)
        assert client.mb_read(0, 1)[0] & 0b100
        client.mb_write(0, 1, bytearray([0]))
        time.sleep(0.1)
        assert adapter.read_tag(tags[1]) is False
    finally:
        try:
            client.disconnect()
            client.destroy()
        except Exception:
            pass
        adapter.stop()
