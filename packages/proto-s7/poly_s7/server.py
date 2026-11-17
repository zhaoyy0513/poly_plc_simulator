"""S7 server adapter.

python-snap7 >= 3 ships a pure-Python S7 server, so no native snap7.dll is
needed (that is a client-side dependency in older versions). Areas are plain
bytearrays we own; the server reads/writes them directly.
"""

from __future__ import annotations

import threading
from typing import Any

from poly_contracts import DataType, ServerConfig, TagDef
from poly_core import ProtocolAdapter, from_bytes, s7_area_sizes, to_bytes

from snap7.server import Server
from snap7.type import SrvArea

_AREA_MAP = {
    "pe": SrvArea.PE,
    "pa": SrvArea.PA,
    "m": SrvArea.MK,
    "i": SrvArea.PE,  # "i" is the German-style input name; keep it as an alias
    "q": SrvArea.PA,
}


class S7Adapter(ProtocolAdapter):
    protocol = "s7"

    def __init__(self, config: ServerConfig) -> None:
        super().__init__(config)
        self._lock = threading.RLock()
        self._server = Server(max_clients=32)
        self._buffers: dict[tuple[str, int], bytearray] = {}
        for tag in config.tags:
            key = (tag.area, tag.db_number or 0)
            if key in self._buffers:
                continue
            self._buffers[key] = bytearray(s7_area_sizes(config.tags)[key])

    def start(self) -> None:
        for (area, index), buffer in self._buffers.items():
            srv_area = SrvArea.DB if area == "db" else _AREA_MAP[area]
            self._server.register_area(srv_area, index, buffer)
        self._server.start_to(self.config.bind_ip, self.config.port)

    def stop(self) -> None:
        try:
            self._server.stop()
        finally:
            self._server.destroy()

    def _buffer(self, tag: TagDef) -> bytearray:
        key = (tag.area, tag.db_number or 0)
        try:
            return self._buffers[key]
        except KeyError:  # tag added after start: grow on demand
            buffer = bytearray(max(256, tag.address + 64))
            self._buffers[key] = buffer
            srv_area = SrvArea.DB if tag.area == "db" else _AREA_MAP[tag.area]
            self._server.register_area(srv_area, key[1], buffer)
            return buffer

    def read_tag(self, tag: TagDef) -> Any:
        with self._lock:
            buffer = self._buffer(tag)
            if tag.data_type is DataType.BOOL:
                bit = tag.bit or 0
                return bool(buffer[tag.address] & (1 << bit))
            size = len(to_bytes(0, tag.data_type, tag.word_order))
            if tag.address + size > len(buffer):
                return None
            return from_bytes(buffer[tag.address : tag.address + size], tag.data_type, tag.word_order)

    def write_tag(self, tag: TagDef, value: Any) -> None:
        with self._lock:
            buffer = self._buffer(tag)
            if tag.data_type is DataType.BOOL:
                bit = tag.bit or 0
                if value:
                    buffer[tag.address] |= 1 << bit
                else:
                    buffer[tag.address] &= ~(1 << bit) & 0xFF
                return
            raw = to_bytes(value, tag.data_type, tag.word_order)
            buffer[tag.address : tag.address + len(raw)] = raw

    def reset(self) -> None:
        with self._lock:
            for buffer in self._buffers.values():
                # Zero in place: snap7's register_area keeps a reference to this
                # exact bytearray, so swapping in a new one would leave the
                # server serving the old (stale) memory.
                buffer[:] = bytes(len(buffer))

    def snapshot(self) -> dict[str, Any]:
        try:
            self.clients = int(getattr(self._server, "client_count", 0))
        except Exception:
            pass
        return super().snapshot()
