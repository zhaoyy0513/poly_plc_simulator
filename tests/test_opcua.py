from __future__ import annotations

import asyncio

from poly_contracts import DataType, Protocol, TagDef
from poly_opcua import OpcUaAdapter

from .util import make_config


def test_opcua_read_write() -> None:
    tags = [
        TagDef(name="temperature", data_type=DataType.FLOAT32, area="opcua"),
        TagDef(name="count", data_type=DataType.UINT16, area="opcua"),
    ]
    config = make_config(Protocol.OPCUA, tags, name="LineA")
    adapter = OpcUaAdapter(config)
    adapter.start()
    try:
        adapter.write_tag(tags[0], 33.5)
        assert abs(adapter.read_tag(tags[0]) - 33.5) < 0.001

        async def client_check() -> None:
            from asyncua import Client, ua

            async with Client(adapter.endpoint_url()) as client:
                index = await client.get_namespace_index("urn:poly:plc")
                node = client.get_node(ua.NodeId("count", index))
                assert await node.read_value() == 0
                await node.write_value(ua.Variant(777, ua.VariantType.UInt16))
                assert adapter.read_tag(tags[1]) == 777

        asyncio.run(client_check())
    finally:
        adapter.stop()


def test_opcua_tag_description_is_written() -> None:
    """asyncua 2.x removed Node.write_description; a tag carrying a description
    used to crash adapter startup (first exposed by the AI point-table import)."""
    tags = [
        TagDef(name="temperature", data_type=DataType.FLOAT32, area="opcua", description="罐区温度"),
        TagDef(name="level", data_type=DataType.FLOAT64, area="opcua", writable=True, description="液位高度"),
    ]
    config = make_config(Protocol.OPCUA, tags, name="DescCheck")
    adapter = OpcUaAdapter(config)
    adapter.start()
    try:

        async def client_check() -> None:
            from asyncua import Client, ua

            async with Client(adapter.endpoint_url()) as client:
                index = await client.get_namespace_index("urn:poly:plc")
                node = client.get_node(ua.NodeId("temperature", index))
                description = await node.read_description()
                assert description.Text == "罐区温度"

        asyncio.run(client_check())
    finally:
        adapter.stop()
