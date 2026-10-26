"""S7 client - the mirror image of the S7 server adapter.

The link feature needs a client that behaves like any other link transport:
one long-lived connection, explicit connect/probe/close, and tag reads/writes
that convert between Python values and the S7 byte areas. `python-snap7` 3.x
ships a pure-Python server and the client is usable on Windows and Linux, so
there is no platform guard here; availability is decided by the environment
that imports this module.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from poly_contracts import DataType, TagDef
from poly_core import byte_size, from_bytes, to_bytes

from snap7.client import (
    Client,
    S7ConnectionError,
    S7Error,
    S7ProtocolError,
    S7StalePacketError,
    S7TimeoutError,
)
from snap7.type import Parameter


class S7LinkError(RuntimeError):
    """Base error for a link operation."""


class S7LinkConnectionError(S7LinkError):
    """The link could not reach the device, or lost the connection."""


class S7LinkDeviceError(S7LinkError):
    """The device answered, but refused the tag/address request."""


# Order matters: protocol errors are answers from a live device, everything
# else in this module is a transport problem.
_DEVICE_ERRORS = (S7ProtocolError, S7StalePacketError)
_CONNECTION_ERRORS = (S7ConnectionError, S7TimeoutError, OSError)


class S7Client:
    """One S7 connection, with the little bit of state a link needs."""

    protocol = "s7"

    def __init__(self) -> None:
        self._client: Client | None = None
        self._host = ""
        self._port = 102
        self._rack = 0
        self._slot = 1
        self._timeout = 3.0
        self._lock = threading.RLock()
        self.last_latency_ms: int | None = None

    # ------------------------------------------------------------------ life
    def configure(
        self, host: str, port: int, rack: int = 0, slot: int = 1, timeout: float = 3.0
    ) -> None:
        """Remember where to connect. Does not open a socket yet."""
        with self._lock:
            if (host, port, rack, slot) != (self._host, self._port, self._rack, self._slot):
                self.close()
            self._host = host
            self._port = port
            self._rack = rack
            self._slot = slot
            self._timeout = max(0.2, float(timeout))

    def probe(self) -> int:
        """Prove the device answers. Returns the connect round trip in ms."""
        with self._lock:
            started = time.monotonic()
            try:
                self._ensure_connected()
            except S7LinkError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                self._drop()
                raise S7LinkConnectionError(f"{self.endpoint}: {exc}") from exc
            self.last_latency_ms = int((time.monotonic() - started) * 1000)
            return self.last_latency_ms

    def close(self) -> None:
        with self._lock:
            self._drop()

    @property
    def connected(self) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            return bool(client.get_connected())
        except Exception:
            return False

    @property
    def endpoint(self) -> str:
        return f"s7://{self._host}:{self._port}"

    # ------------------------------------------------------------------ data
    def read_tags(self, tags: list[TagDef]) -> dict[str, Any]:
        """Read a tag table, one tag at a time.

        A bad address is a device answer; keep the other tags visible and let
        the caller see a ``None`` for the one that could not be read. A lost
        connection aborts the snapshot and is reported as a link failure.
        """
        result: dict[str, Any] = {}
        with self._lock:
            self._ensure_connected()
            for tag in tags:
                try:
                    result[tag.name] = self._read_one(tag)
                except S7LinkDeviceError:
                    result[tag.name] = None
        return result

    def read_tag(self, tag: TagDef) -> Any:
        with self._lock:
            self._ensure_connected()
            return self._read_one(tag)

    def write_tag(self, tag: TagDef, value: Any) -> None:
        with self._lock:
            self._ensure_connected()
            started = time.monotonic()
            try:
                if tag.data_type is DataType.BOOL:
                    current = self._read_bytes(tag, 1)
                    bit = tag.bit or 0
                    if value:
                        current[0] |= 1 << bit
                    else:
                        current[0] &= ~(1 << bit) & 0xFF
                    self._write_bytes(tag, current)
                else:
                    self._write_bytes(tag, bytearray(to_bytes(value, tag.data_type, tag.word_order)))
                self.last_latency_ms = int((time.monotonic() - started) * 1000)
            except S7LinkError:
                raise
            except _DEVICE_ERRORS as exc:
                raise S7LinkDeviceError(str(exc)) from exc
            except _CONNECTION_ERRORS as exc:
                self._drop()
                raise S7LinkConnectionError(f"{self.endpoint}: {exc}") from exc

    # ------------------------------------------------------------------ internals
    def _ensure_connected(self) -> None:
        if self.connected:
            return
        self._drop()
        if not self._host:
            raise S7LinkConnectionError("还没有填目标地址")
        client = Client()
        timeout_ms = max(200, int(self._timeout * 1000))
        try:
            client.set_param(Parameter.SendTimeout, timeout_ms)
            client.set_param(Parameter.RecvTimeout, timeout_ms)
        except Exception:
            pass
        try:
            client.connect(self._host, self._rack, self._slot, tcp_port=self._port)
        except _DEVICE_ERRORS as exc:
            try:
                client.destroy()
            except Exception:
                pass
            raise S7LinkDeviceError(str(exc)) from exc
        except (S7Error, OSError) as exc:
            try:
                client.destroy()
            except Exception:
                pass
            raise S7LinkConnectionError(f"{self.endpoint}: {exc}") from exc
        self._client = client

    def _drop(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.destroy()
        except Exception:
            pass

    def _read_one(self, tag: TagDef) -> Any:
        started = time.monotonic()
        try:
            if tag.data_type is DataType.BOOL:
                raw = self._read_bytes(tag, 1)
                value = bool(raw[0] & (1 << (tag.bit or 0)))
            else:
                raw = self._read_bytes(tag, byte_size(tag.data_type))
                value = from_bytes(raw, tag.data_type, tag.word_order)
            self.last_latency_ms = int((time.monotonic() - started) * 1000)
            return value
        except S7LinkError:
            raise
        except _DEVICE_ERRORS as exc:
            raise S7LinkDeviceError(f"{tag.name}: {exc}") from exc
        except _CONNECTION_ERRORS as exc:
            self._drop()
            raise S7LinkConnectionError(f"{self.endpoint}: {exc}") from exc

    def _read_bytes(self, tag: TagDef, size: int) -> bytearray:
        client = self._client
        if client is None:
            raise S7LinkConnectionError("连接已断开")
        if tag.area == "db":
            return client.db_read(tag.db_number or 0, tag.address, size)
        if tag.area == "m":
            return client.mb_read(tag.address, size)
        if tag.area == "i":
            return client.eb_read(tag.address, size)
        if tag.area == "q":
            return client.ab_read(tag.address, size)
        raise S7LinkDeviceError(f"未知 S7 区域: {tag.area}")

    def _write_bytes(self, tag: TagDef, data: bytearray) -> None:
        client = self._client
        if client is None:
            raise S7LinkConnectionError("连接已断开")
        if tag.area == "db":
            client.db_write(tag.db_number or 0, tag.address, data)
        elif tag.area == "m":
            client.mb_write(tag.address, len(data), data)
        elif tag.area == "i":
            client.eb_write(tag.address, len(data), data)
        elif tag.area == "q":
            client.ab_write(tag.address, data)
        else:
            raise S7LinkDeviceError(f"未知 S7 区域: {tag.area}")
