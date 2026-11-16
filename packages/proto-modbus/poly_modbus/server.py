"""Modbus TCP server implemented directly on top of sockets.

Why not pymodbus: its datastore API changed incompatibly in 3.16 (the old
context classes are deprecated for removal in 4.x). The wire format we need is
small and stable, and owning it keeps address mapping 0-based and exact.

Supported function codes: 1, 2, 3, 4, 5, 6, 15, 16.

There is deliberately no tag table: a Modbus instance is nothing but its four
raw data areas, and what each address means is decided by the client that
connects to it. Nothing here moves values on its own either - the simulator
serves whatever is in the areas, whoever wrote them.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from poly_contracts import ServerConfig
from poly_core import ProtocolAdapter, bind_for_listening

MBAP = struct.Struct(">HHHB")


@dataclass
class _ClientConn:
    id: str
    conn: socket.socket
    remote: str
    connected_at: float
    last_activity: float
    unit_id: int | None = None
    requests: int = 0

# Modbus addresses are 16 bit wide, so a compliant client may address anything
# in 0..65535 even though nothing has been configured. Always allocate the full
# space by default: scanning tools poll fixed ranges, and a smaller window
# answers them with illegal-address exceptions.
ADDRESS_SPACE = 65536


def area_size(config: ServerConfig) -> int:
    """How many points each area holds: the whole space unless configured.

    A configured size is not a cosmetic limit - it is what makes the instance
    answer ILLEGAL_ADDRESS past its map, exactly like the device it stands in
    for. Allocation and the wire checks both read this, so there is no way to
    configure 100 points and still have a client read 500.
    """
    limit = config.options.register_count
    if not limit:
        return ADDRESS_SPACE
    return max(1, min(int(limit), ADDRESS_SPACE))

ILLEGAL_FUNCTION = 0x01
ILLEGAL_ADDRESS = 0x02
ILLEGAL_VALUE = 0x03
SERVER_FAILURE = 0x04


class ModbusTcpAdapter(ProtocolAdapter):
    protocol = "modbus_tcp"

    def __init__(self, config: ServerConfig) -> None:
        super().__init__(config)
        self._lock = threading.RLock()
        # Sized by the instance's own configuration: a shorter area means every
        # existing bounds check (read/write, single/multiple) already answers
        # ILLEGAL_ADDRESS past the end.
        self.area_size = area_size(config)
        self._holding = [0] * self.area_size
        self._input = [0] * self.area_size
        self._coils = [False] * self.area_size
        self._discrete = [False] * self.area_size
        self._sock: socket.socket | None = None
        self._running = False
        self._threads: list[threading.Thread] = []
        self._clients_lock = threading.Lock()
        self._conn_records: dict[str, _ClientConn] = {}

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Exclusive on Windows, TIME_WAIT reuse on POSIX. A bare SO_REUSEADDR
        # here would let two instances claim the same port on Windows and both
        # look healthy while one of them never sees a request.
        bind_for_listening(sock)
        sock.bind((self.config.bind_ip, self.config.port))
        sock.listen(64)
        sock.settimeout(0.5)
        self._sock = sock
        self._running = True
        thread = threading.Thread(target=self._accept_loop, name="modbus-accept", daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # ------------------------------------------------------------------ data
    def _area(self, name: str) -> list:
        return {
            "holding": self._holding,
            "input": self._input,
            "coil": self._coils,
            "discrete": self._discrete,
        }[name]

    def snapshot(self) -> dict[str, Any]:
        """No named values: use read_area() for the current data plane."""
        return {}

    def read_area(self, area: str, address: int, count: int) -> list[Any]:
        """Read raw registers/coils without going through the tag table."""
        if area not in ("holding", "input", "coil", "discrete"):
            raise ValueError(f"unknown area: {area}")
        data = self._area(area)
        if address < 0 or count < 1 or address + count > len(data):
            raise ValueError(
                f"address out of range: {address}+{count} (size {len(data)})"
            )
        with self._lock:
            values = data[address : address + count]
        if area in ("coil", "discrete"):
            return [bool(value) for value in values]
        return [int(value) & 0xFFFF for value in values]

    def write_area(self, area: str, address: int, value: Any) -> None:
        """Set one raw register/bit.

        This is the admin path, not a Modbus client path: input registers and
        discrete inputs are read-only on the wire, but the simulator lets the
        UI preset them so a client can read the values it expects.
        """
        if area not in ("holding", "input", "coil", "discrete"):
            raise ValueError(f"unknown area: {area}")
        data = self._area(area)
        if address < 0 or address >= len(data):
            raise ValueError(f"address out of range: {address} (size {len(data)})")

        if area in ("coil", "discrete"):
            if isinstance(value, bool):
                bit = value
            elif isinstance(value, int) and value in (0, 1):
                bit = value == 1
            else:
                raise ValueError("bit value must be 0 or 1")
            with self._lock:
                data[address] = bit
            return

        if isinstance(value, bool):
            number = int(value)
        elif isinstance(value, int):
            number = value
        elif isinstance(value, float) and value.is_integer():
            number = int(value)
        else:
            raise ValueError("register value must be an integer")
        if not 0 <= number <= 0xFFFF:
            raise ValueError("register value must be between 0 and 65535")
        with self._lock:
            data[address] = number

    def connections(self) -> list[dict[str, Any]]:
        with self._clients_lock:
            return [
                {
                    "id": record.id,
                    "remote": record.remote,
                    "connected_at": record.connected_at,
                    "last_activity": record.last_activity,
                    "unit_id": record.unit_id,
                    "requests": record.requests,
                }
                for record in self._conn_records.values()
            ]

    def kick(self, connection_id: str) -> None:
        with self._clients_lock:
            record = self._conn_records.get(connection_id)
            if record is None:
                raise KeyError(f"unknown connection: {connection_id}")
            try:
                record.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            record.conn.close()

    def endpoint(self) -> str:
        return f"modbus-tcp://{self.config.bind_ip}:{self.config.port}"

    # ------------------------------------------------------------------ socket
    def _accept_loop(self) -> None:
        while self._running and self._sock is not None:
            try:
                client, address = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._clients_lock:
                self.clients += 1
                conn_id = uuid.uuid4().hex[:8]
                now = time.time()
                self._conn_records[conn_id] = _ClientConn(
                    id=conn_id,
                    conn=client,
                    remote=f"{address[0]}:{address[1]}",
                    connected_at=now,
                    last_activity=now,
                )
            thread = threading.Thread(
                target=self._serve, args=(client, conn_id), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int) -> bytes | None:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = conn.recv(size - len(chunks))
            if not chunk:
                return None
            chunks.extend(chunk)
        return bytes(chunks)

    def _serve(self, conn: socket.socket, conn_id: str) -> None:
        conn.settimeout(180)
        try:
            while self._running:
                header = self._recv_exact(conn, 7)
                if header is None:
                    break
                tid, pid, length, unit = MBAP.unpack(header)
                with self._clients_lock:
                    record = self._conn_records.get(conn_id)
                    if record is not None:
                        record.last_activity = time.time()
                        record.unit_id = unit
                        record.requests += 1
                if pid != 0 or length < 2 or length > 260:
                    break
                pdu = self._recv_exact(conn, length - 1)
                if pdu is None:
                    break
                response = self._handle(pdu)
                conn.sendall(MBAP.pack(tid, 0, len(response) + 1, unit) + response)
        except (socket.timeout, ConnectionError, OSError):
            pass
        finally:
            try:
                conn.close()
            finally:
                with self._clients_lock:
                    self._conn_records.pop(conn_id, None)
                    self.clients = max(0, self.clients - 1)

    # ------------------------------------------------------------------ pdu
    def _handle(self, pdu: bytes) -> bytes:
        function = pdu[0]
        try:
            if function == 1:
                return self._read_bits(pdu, self._coils)
            if function == 2:
                return self._read_bits(pdu, self._discrete)
            if function == 3:
                return self._read_registers(pdu, self._holding)
            if function == 4:
                return self._read_registers(pdu, self._input)
            if function == 5:
                return self._write_single_coil(pdu)
            if function == 6:
                return self._write_single_register(pdu)
            if function == 15:
                return self._write_multiple_coils(pdu)
            if function == 16:
                return self._write_multiple_registers(pdu)
            return bytes([function | 0x80, ILLEGAL_FUNCTION])
        except _ModbusError as exc:
            return bytes([function | 0x80, exc.code])
        except Exception:
            return bytes([function | 0x80, SERVER_FAILURE])

    def _read_bits(self, pdu: bytes, area: list[bool]) -> bytes:
        if len(pdu) < 5:
            raise _ModbusError(ILLEGAL_VALUE)
        address, count = struct.unpack(">HH", pdu[1:5])
        if not 1 <= count <= 2000:
            raise _ModbusError(ILLEGAL_VALUE)
        with self._lock:
            if address + count > len(area):
                raise _ModbusError(ILLEGAL_ADDRESS)
            bits = area[address : address + count]
        payload = bytearray((count + 7) // 8)
        for index, bit in enumerate(bits):
            if bit:
                payload[index // 8] |= 1 << (index % 8)
        return bytes([pdu[0], len(payload)]) + bytes(payload)

    def _read_registers(self, pdu: bytes, area: list[int]) -> bytes:
        if len(pdu) < 5:
            raise _ModbusError(ILLEGAL_VALUE)
        address, count = struct.unpack(">HH", pdu[1:5])
        if not 1 <= count <= 125:
            raise _ModbusError(ILLEGAL_VALUE)
        with self._lock:
            if address + count > len(area):
                raise _ModbusError(ILLEGAL_ADDRESS)
            words = area[address : address + count]
        payload = b"".join(struct.pack(">H", word & 0xFFFF) for word in words)
        return bytes([pdu[0], len(payload)]) + payload

    def _write_single_coil(self, pdu: bytes) -> bytes:
        if len(pdu) < 5:
            raise _ModbusError(ILLEGAL_VALUE)
        address, raw = struct.unpack(">HH", pdu[1:5])
        if raw not in (0x0000, 0xFF00):
            raise _ModbusError(ILLEGAL_VALUE)
        with self._lock:
            if address >= len(self._coils):
                raise _ModbusError(ILLEGAL_ADDRESS)
            self._coils[address] = raw == 0xFF00
        return pdu[:5]

    def _write_single_register(self, pdu: bytes) -> bytes:
        if len(pdu) < 5:
            raise _ModbusError(ILLEGAL_VALUE)
        address, value = struct.unpack(">HH", pdu[1:5])
        with self._lock:
            if address >= len(self._holding):
                raise _ModbusError(ILLEGAL_ADDRESS)
            self._holding[address] = value
        return pdu[:5]

    def _write_multiple_coils(self, pdu: bytes) -> bytes:
        if len(pdu) < 6:
            raise _ModbusError(ILLEGAL_VALUE)
        address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
        if not 1 <= count <= 1968 or byte_count != (count + 7) // 8:
            raise _ModbusError(ILLEGAL_VALUE)
        payload = pdu[6 : 6 + byte_count]
        if len(payload) != byte_count:
            raise _ModbusError(ILLEGAL_VALUE)
        with self._lock:
            if address + count > len(self._coils):
                raise _ModbusError(ILLEGAL_ADDRESS)
            for index in range(count):
                self._coils[address + index] = bool(payload[index // 8] & (1 << (index % 8)))
        return pdu[:5]

    def _write_multiple_registers(self, pdu: bytes) -> bytes:
        if len(pdu) < 6:
            raise _ModbusError(ILLEGAL_VALUE)
        address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
        if not 1 <= count <= 123 or byte_count != count * 2:
            raise _ModbusError(ILLEGAL_VALUE)
        payload = pdu[6 : 6 + byte_count]
        if len(payload) != byte_count:
            raise _ModbusError(ILLEGAL_VALUE)
        words = list(struct.unpack(f">{count}H", payload))
        with self._lock:
            if address + count > len(self._holding):
                raise _ModbusError(ILLEGAL_ADDRESS)
            self._holding[address : address + count] = words
        return pdu[:5]


class _ModbusError(Exception):
    def __init__(self, code: int) -> None:
        self.code = code
