"""Adapter interface shared by every protocol implementation."""

from __future__ import annotations

from typing import Any

from poly_contracts import ServerConfig, TagDef


class ProtocolAdapter:
    """One running protocol endpoint.

    Implementations own their native data plane (registers / byte areas / UA
    nodes); the tag table is only a *view* on top of it. That keeps writes made
    by external clients visible without a second source of truth.
    """

    protocol: str = ""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.tags: dict[str, TagDef] = {tag.name: tag for tag in config.tags}
        self.clients = 0

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - best effort
        raise NotImplementedError

    def read_tag(self, tag: TagDef) -> Any:
        raise NotImplementedError

    def write_tag(self, tag: TagDef, value: Any) -> None:
        raise NotImplementedError

    def read_area(self, area: str, address: int, count: int) -> list[Any]:
        """Raw data-plane read used by the UI register viewer.

        Unlike tag reads this does not depend on the configured tag table: a
        client can address the whole native space. Protocols without a
        register/byte-area model may leave this unimplemented.
        """
        raise NotImplementedError(f"{self.protocol} does not expose raw areas")

    def write_area(self, area: str, address: int, value: Any) -> None:
        """Write one raw register/bit without going through the tag table.

        Only the protocols that expose a raw area model need to implement this.
        The simulator UI uses it to preset values that a Modbus client cannot
        write itself (discrete inputs and input registers are read-only on the
        wire).
        """
        raise NotImplementedError(f"{self.protocol} does not support raw area writes")

    def snapshot(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, tag in self.tags.items():
            try:
                values[name] = self.read_tag(tag)
            except Exception:  # a single bad tag must not break the snapshot
                values[name] = None
        return values

    def connections(self) -> list[dict[str, Any]]:
        """Live client connections. Protocols without a per-client model return []."""
        return []

    def kick(self, connection_id: str) -> None:
        """Drop one client connection. Not every protocol can do this."""
        raise NotImplementedError(f"{self.protocol} does not support kicking clients")

    def reset(self) -> None:
        """Zero the data plane in place, keeping client connections alive."""
        raise NotImplementedError(f"{self.protocol} does not support reset")

    def endpoint(self) -> str:
        return f"{self.config.protocol.value}://{self.config.bind_ip}:{self.config.port}"
