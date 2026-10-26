"""Protocol-specific link transports.

`LinkSupervisor` deliberately knows nothing about MBAP frames, S7 areas or
exception taxonomies. It owns the lifecycle (one cached connection, backoff,
event history) and talks to one of these small adapters, which normalize
"the device refused" and "the device is not there" into two exception types.

Keeping the adapters here instead of in `poly_modbus` / `poly_s7` means the
protocol packages stay server-side libraries; only the control plane - the
component that actually acts as a client - imports them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol as TypingProtocol

from poly_contracts import Protocol, TagDef
from poly_modbus import ModbusError, ModbusException, ModbusReadOnlyError, ModbusTcpClient
from poly_opcua import (
    OpcUaClient,
    OpcUaLinkConnectionError,
    OpcUaLinkDeviceError,
)
from poly_s7 import (
    S7Client,
    S7LinkConnectionError,
    S7LinkDeviceError,
)


class LinkError(RuntimeError):
    """Anything a link operation can fail with."""


class LinkStoppedError(LinkError):
    """The instance is disabled; a read must not silently reconnect it."""


class LinkConnectionError(LinkError):
    """The link could not reach the device, or lost the connection."""


class LinkDeviceError(LinkError):
    """The device answered and refused the request. The link is still alive."""


@dataclass
class LinkTarget:
    """Where a link points, and how to speak to it."""

    host: str
    port: int = 502
    protocol: Protocol = Protocol.MODBUS_TCP
    unit_id: int = 1
    rack: int = 0
    slot: int = 1
    timeout: float = 3.0
    tags: list[TagDef] = field(default_factory=list)
    # OPC UA only: the namespace the bare tag names live in on the far side.
    namespace_index: int = 2

    @property
    def endpoint(self) -> str:
        if self.protocol is Protocol.S7:
            return f"s7://{self.host}:{self.port}"
        if self.protocol is Protocol.OPCUA:
            return f"opc.tcp://{self.host}:{self.port}"
        return f"modbus-tcp://{self.host}:{self.port}"


class LinkClient(TypingProtocol):
    protocol: str

    def configure(self, target: LinkTarget) -> None: ...
    def probe(self) -> int: ...
    def close(self) -> None: ...
    def dispose(self) -> None:
        """The instance is gone; release anything `close()` leaves standing."""
        ...
    def read_tags(self, tags: list[TagDef]) -> dict[str, Any]: ...
    def write_tag(self, tag: TagDef, value: Any) -> None: ...
    def read_area(self, area: str, address: int, count: int) -> list[Any]: ...
    def write_area(self, area: str, address: int, value: Any) -> None: ...
    @property
    def endpoint(self) -> str: ...
    @property
    def connected(self) -> bool: ...
    @property
    def last_latency_ms(self) -> int | None: ...
    @property
    def namespaces(self) -> list[str]:
        """Namespaces the far side declares; empty for protocols without them."""
        ...


class ModbusLinkClient:
    """Modbus TCP adapter around the existing hand-written client."""

    protocol = "modbus_tcp"

    def __init__(self, client: ModbusTcpClient | None = None) -> None:
        self._client = client or ModbusTcpClient("", 502)

    def configure(self, target: LinkTarget) -> None:
        self._client.configure(target.host, target.port, target.unit_id, target.timeout)

    def probe(self) -> int:
        try:
            return self._client.probe()
        except ModbusException as exc:
            # A Modbus exception is an answer: the device is there and spoke
            # the protocol. Keep the same distinction the supervisor has always
            # made, even though the bytes came from a different adapter.
            raise LinkDeviceError(str(exc)) from exc
        except (ModbusError, OSError) as exc:
            raise LinkConnectionError(str(exc)) from exc

    def close(self) -> None:
        self._client.close()

    def dispose(self) -> None:
        """Nothing outlives `close()`: the socket is the whole resource."""
        self.close()

    def read_tags(self, tags: list[TagDef]) -> dict[str, Any]:
        raise LinkDeviceError("连接实例没有标签表，请在数据区里按地址读写")

    def write_tag(self, tag: TagDef, value: Any) -> None:
        raise LinkDeviceError("连接实例没有标签表，请在数据区里按地址写入")

    def read_area(self, area: str, address: int, count: int) -> list[Any]:
        try:
            return self._client.read_area(area, address, count)
        except (ModbusException, ModbusReadOnlyError) as exc:
            raise LinkDeviceError(str(exc)) from exc
        except (ModbusError, OSError) as exc:
            raise LinkConnectionError(str(exc)) from exc

    def write_area(self, area: str, address: int, value: Any) -> None:
        try:
            self._client.write_area(area, address, value)
        except (ModbusException, ModbusReadOnlyError) as exc:
            raise LinkDeviceError(str(exc)) from exc
        except (ModbusError, OSError) as exc:
            raise LinkConnectionError(str(exc)) from exc

    @property
    def endpoint(self) -> str:
        return self._client.endpoint

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def last_latency_ms(self) -> int | None:
        return self._client.last_latency_ms

    @property
    def namespaces(self) -> list[str]:
        """Modbus has no namespace concept; the dialog shows nothing extra."""
        return []


class S7LinkClient:
    """S7 adapter around `poly_s7.S7Client`."""

    protocol = "s7"

    def __init__(self) -> None:
        self._client = S7Client()

    def configure(self, target: LinkTarget) -> None:
        self._client.configure(
            target.host,
            target.port,
            rack=target.rack,
            slot=target.slot,
            timeout=target.timeout,
        )

    def probe(self) -> int:
        try:
            return self._client.probe()
        except S7LinkDeviceError as exc:
            raise LinkDeviceError(str(exc)) from exc
        except S7LinkConnectionError as exc:
            raise LinkConnectionError(str(exc)) from exc

    def close(self) -> None:
        self._client.close()

    def dispose(self) -> None:
        """Nothing outlives `close()`: the socket is the whole resource."""
        self.close()

    def read_tags(self, tags: list[TagDef]) -> dict[str, Any]:
        try:
            return self._client.read_tags(tags)
        except S7LinkDeviceError as exc:
            raise LinkDeviceError(str(exc)) from exc
        except S7LinkConnectionError as exc:
            raise LinkConnectionError(str(exc)) from exc

    def write_tag(self, tag: TagDef, value: Any) -> None:
        try:
            self._client.write_tag(tag, value)
        except S7LinkDeviceError as exc:
            raise LinkDeviceError(str(exc)) from exc
        except S7LinkConnectionError as exc:
            raise LinkConnectionError(str(exc)) from exc

    def read_area(self, area: str, address: int, count: int) -> list[Any]:
        raise LinkDeviceError("S7 连接实例按标签表读写，不提供原始数据区")

    def write_area(self, area: str, address: int, value: Any) -> None:
        raise LinkDeviceError("S7 连接实例按标签表读写，不提供原始数据区")

    @property
    def endpoint(self) -> str:
        return self._client.endpoint

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def last_latency_ms(self) -> int | None:
        return self._client.last_latency_ms

    @property
    def namespaces(self) -> list[str]:
        """S7 has no namespace concept; the dialog shows nothing extra."""
        return []


class OpcUaLinkClient:
    """OPC UA adapter around `poly_opcua.OpcUaClient`.

    The async-to-sync bridge lives inside that client, so this class only does
    what the other two adapters do: normalize failures and declare which view
    of the far side exists. OPC UA addresses named nodes, so - exactly like S7 -
    there is no raw data area to fall back on.
    """

    protocol = "opcua"

    def __init__(self) -> None:
        self._client = OpcUaClient()

    def configure(self, target: LinkTarget) -> None:
        self._client.configure(
            target.host,
            target.port,
            namespace_index=target.namespace_index,
            timeout=target.timeout,
        )

    def probe(self) -> int:
        try:
            return self._client.probe()
        except OpcUaLinkDeviceError as exc:
            raise LinkDeviceError(str(exc)) from exc
        except OpcUaLinkConnectionError as exc:
            raise LinkConnectionError(str(exc)) from exc

    def close(self) -> None:
        self._client.close()

    def dispose(self) -> None:
        """Release the event loop too - the instance is being deleted."""
        self._client.dispose()

    def read_tags(self, tags: list[TagDef]) -> dict[str, Any]:
        try:
            return self._client.read_tags(tags)
        except OpcUaLinkDeviceError as exc:
            raise LinkDeviceError(str(exc)) from exc
        except OpcUaLinkConnectionError as exc:
            raise LinkConnectionError(str(exc)) from exc

    def write_tag(self, tag: TagDef, value: Any) -> None:
        try:
            self._client.write_tag(tag, value)
        except OpcUaLinkDeviceError as exc:
            raise LinkDeviceError(str(exc)) from exc
        except OpcUaLinkConnectionError as exc:
            raise LinkConnectionError(str(exc)) from exc

    def read_area(self, area: str, address: int, count: int) -> list[Any]:
        raise LinkDeviceError("OPC UA 连接实例按标签表读写，不提供原始数据区")

    def write_area(self, area: str, address: int, value: Any) -> None:
        raise LinkDeviceError("OPC UA 连接实例按标签表读写，不提供原始数据区")

    @property
    def endpoint(self) -> str:
        return self._client.endpoint

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def last_latency_ms(self) -> int | None:
        return self._client.last_latency_ms

    @property
    def namespaces(self) -> list[str]:
        """What the far server declares, read once per session by `probe`."""
        return self._client.namespaces


def build_link_client(protocol: Protocol) -> LinkClient:
    if protocol is Protocol.MODBUS_TCP:
        return ModbusLinkClient()
    if protocol is Protocol.S7:
        return S7LinkClient()
    if protocol is Protocol.OPCUA:
        return OpcUaLinkClient()
    raise LinkError(f"连接实例暂不支持 {protocol.value}；目前支持 Modbus TCP、S7 与 OPC UA")
