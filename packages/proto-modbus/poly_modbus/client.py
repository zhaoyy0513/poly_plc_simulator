"""Modbus TCP client - how the platform reaches somebody else's server.

This is the mirror image of `server.py`: same wire format, same MBAP header,
same exception codes, so the two halves of the product agree on what a frame
means. Written by hand for the same reason - pymodbus's datastore churn proved
that owning 200 lines of stable framing beats inheriting somebody else's.

What it adds over the server: a connection that can drop. Idle gateways and
PLC link timeouts close sockets underneath us, so every operation retries once
on a fresh connection before giving up. That matters here in a way it never did
for a local simulator: a stale socket must not look like "the device said no".
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Any

MBAP = struct.Struct(">HHHB")

ILLEGAL_FUNCTION = 0x01
ILLEGAL_ADDRESS = 0x02
ILLEGAL_VALUE = 0x03
SERVER_FAILURE = 0x04
ACKNOWLEDGE = 0x05
SERVER_BUSY = 0x06
MEMORY_PARITY = 0x08
GATEWAY_PATH = 0x0A
GATEWAY_TARGET = 0x0B

_EXCEPTIONS = {
    ILLEGAL_FUNCTION: "设备不支持这个功能码",
    ILLEGAL_ADDRESS: "地址越界，设备拒绝响应（该地址不存在）",
    ILLEGAL_VALUE: "数值不合法，设备拒绝响应",
    SERVER_FAILURE: "设备内部执行失败",
    ACKNOWLEDGE: "设备已接受请求但需要较长时间处理",
    SERVER_BUSY: "设备正忙，请稍后重试",
    MEMORY_PARITY: "设备内存校验失败",
    GATEWAY_PATH: "网关路径不可用",
    GATEWAY_TARGET: "网关后面的目标设备无响应",
}

READ_ONLY_AREAS = ("input", "discrete")


class ModbusError(RuntimeError):
    """A failure that is about the *link*, not about a bad address."""


class ModbusReadOnlyError(ModbusError):
    """The area cannot be written on the wire, so nothing was ever sent.

    Kept apart from `ModbusError` because the caller must be able to tell "the
    link is broken" from "you asked for something the protocol does not allow" -
    only the first one says anything about the connection.
    """


class ModbusException(ModbusError):
    """The device answered with a Modbus exception code."""

    def __init__(self, function: int, code: int) -> None:
        self.function = function
        self.code = code
        super().__init__(_EXCEPTIONS.get(code, f"设备返回异常码 0x{code:02X}"))


class ModbusTcpClient:
    """One TCP connection to one Modbus server, guarded by a lock.

    Not thread-safe by itself; every call takes the lock so the control plane
    can serve a page read and a console write at the same time without
    interleaving frames on one socket.
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 1,
        timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._lock = threading.RLock()
        self._sock: socket.socket | None = None
        self._tid = 0
        self.last_latency_ms: int | None = None
        self.last_ok_at: float | None = None

    # ------------------------------------------------------------------ link
    @property
    def endpoint(self) -> str:
        return f"modbus-tcp://{self.host}:{self.port}"

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def configure(self, host: str, port: int, unit_id: int, timeout: float) -> None:
        """Re-point this client without dropping the supervisor around it.

        Called when a link instance is edited: the target may move, but the
        object the control plane holds a reference to must stay the same one.
        """
        with self._lock:
            if (host, port, unit_id) != (self.host, self.port, self.unit_id):
                self._close_locked()
            self.host, self.port, self.unit_id = host, port, unit_id
            self.timeout = timeout

    def connect(self) -> None:
        with self._lock:
            self._close_locked()
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            except socket.timeout as exc:
                raise ModbusError(f"连接 {self.host}:{self.port} 超时（{self.timeout:g}s）") from exc
            except ConnectionRefusedError as exc:
                raise ModbusError(f"{self.host}:{self.port} 拒绝连接（没有服务在听这个端口？）") from exc
            except OSError as exc:
                raise ModbusError(f"连接 {self.host}:{self.port} 失败: {exc}") from exc
            sock.settimeout(self.timeout)
            self._sock = sock

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def probe(self) -> int:
        """Round trip that says whether the device answers. Returns latency ms."""
        self.connect()
        started = time.monotonic()
        self.read_holding_registers(0, 1)
        return int((time.monotonic() - started) * 1000)

    # ------------------------------------------------------------------ data
    def read_area(self, area: str, address: int, count: int) -> list[Any]:
        if area == "holding":
            return self.read_holding_registers(address, count)
        if area == "input":
            return self.read_input_registers(address, count)
        if area == "coil":
            return self.read_coils(address, count)
        if area == "discrete":
            return self.read_discrete_inputs(address, count)
        raise ValueError(f"unknown area: {area}")

    def write_area(self, area: str, address: int, value: Any) -> None:
        if area == "holding":
            self.write_register(address, int(value))
            return
        if area == "coil":
            self.write_coil(address, bool(int(value)))
            return
        if area in READ_ONLY_AREAS:
            raise ModbusReadOnlyError(
                f"{area} 是只读数据区（FC{'04' if area == 'input' else '02'}），"
                "真实 server 不会接受写入"
            )
        raise ValueError(f"unknown area: {area}")

    def read_coils(self, address: int, count: int) -> list[bool]:
        return self._read_bits(1, address, count)

    def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        return self._read_bits(2, address, count)

    def read_holding_registers(self, address: int, count: int) -> list[int]:
        return self._read_registers(3, address, count)

    def read_input_registers(self, address: int, count: int) -> list[int]:
        return self._read_registers(4, address, count)

    def write_coil(self, address: int, value: bool) -> None:
        self._transact(5, struct.pack(">HH", address, 0xFF00 if value else 0x0000))

    def write_register(self, address: int, value: int) -> None:
        self._transact(6, struct.pack(">HH", address, int(value) & 0xFFFF))

    def write_coils(self, address: int, values: list[bool]) -> None:
        count = len(values)
        payload = bytearray((count + 7) // 8)
        for index, bit in enumerate(values):
            if bit:
                payload[index // 8] |= 1 << (index % 8)
        self._transact(15, struct.pack(">HHB", address, count, len(payload)) + bytes(payload))

    def write_registers(self, address: int, values: list[int]) -> None:
        words = b"".join(struct.pack(">H", int(v) & 0xFFFF) for v in values)
        self._transact(16, struct.pack(">HHB", address, len(values), len(words)) + words)

    # ------------------------------------------------------------------ wire
    def _read_bits(self, function: int, address: int, count: int) -> list[bool]:
        self._check_range(function, address, count, 1, 2000)
        pdu = self._transact(function, struct.pack(">HH", address, count))
        if len(pdu) < 2:
            raise ModbusError("设备响应的长度不对，无法解析")
        byte_count = pdu[1]
        body = pdu[2 : 2 + byte_count]
        if len(body) != byte_count:
            raise ModbusError("设备响应的数据不完整，无法解析")
        return [bool(body[i // 8] & (1 << (i % 8))) for i in range(count)]

    def _read_registers(self, function: int, address: int, count: int) -> list[int]:
        self._check_range(function, address, count, 1, 125)
        pdu = self._transact(function, struct.pack(">HH", address, count))
        if len(pdu) < 2:
            raise ModbusError("设备响应的长度不对，无法解析")
        byte_count = pdu[1]
        body = pdu[2 : 2 + byte_count]
        if len(body) != byte_count or byte_count % 2:
            raise ModbusError("设备响应的数据不完整，无法解析")
        return list(struct.unpack(f">{byte_count // 2}H", body))

    @staticmethod
    def _check_range(function: int, address: int, count: int, low: int, high: int) -> None:
        if not low <= count <= high:
            raise ModbusError(f"一次读取数量需在 {low}–{high} 之间（当前 {count}）")
        if address < 0 or address + count > 65536:
            raise ModbusError(f"地址越界：{address} + {count} > 65536")

    def _transact(self, function: int, payload: bytes) -> bytes:
        """Send one PDU, return the response PDU. Reconnects once on a dead socket."""
        with self._lock:
            self._tid = (self._tid + 1) & 0xFFFF
            frame = MBAP.pack(self._tid, 0, len(payload) + 2, self.unit_id) + bytes([function]) + payload
            try:
                return self._exchange_locked(frame, function)
            except (ModbusError, OSError) as exc:
                if isinstance(exc, ModbusException):
                    raise
                # A dropped socket is not an answer: re-establish and retry once.
                self._close_locked()
                try:
                    self.connect()
                except ModbusError:
                    # Why the call failed is *why the reconnect failed* - the
                    # original error would only say "not connected yet", which
                    # is true of every first call and explains nothing.
                    raise
                return self._exchange_locked(frame, function)

    def _exchange_locked(self, frame: bytes, function: int) -> bytes:
        if self._sock is None:
            raise ModbusError("还没有连接")
        started = time.monotonic()
        try:
            self._sock.sendall(frame)
            header = self._recv_exact(self._sock, 7)
        except socket.timeout as exc:
            raise ModbusError(f"设备 {self.timeout:g}s 内没有响应") from exc
        except OSError as exc:
            raise ModbusError(f"连接中断: {exc}") from exc
        tid, pid, length, _unit = MBAP.unpack(header)
        if pid != 0 or length < 2 or length > 260:
            raise ModbusError("设备返回的帧头不合法")
        pdu = self._recv_exact(self._sock, length - 1)
        if pdu[0] & 0x80:
            raise ModbusException(function, pdu[1] if len(pdu) > 1 else 0)
        if pdu[0] != function:
            raise ModbusError(f"设备返回了意料之外的功能码 {pdu[0]}")
        self.last_latency_ms = int((time.monotonic() - started) * 1000)
        self.last_ok_at = time.time()
        return pdu

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = conn.recv(size - len(chunks))
            if not chunk:
                raise ModbusError("设备关闭了连接")
            chunks.extend(chunk)
        return bytes(chunks)
