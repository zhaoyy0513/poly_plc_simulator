"""Value <-> wire representation helpers.

The canonical in-memory representation is big-endian. ``WordOrder.LITTLE``
swaps 16-bit words (not bytes), which is what most PLC clients mean by
"CD-AB"/"little endian" for 32/64-bit values spread over registers.
"""

from __future__ import annotations

import struct
from typing import Any

from poly_contracts import DataType, WordOrder

_REGISTER_FMT: dict[DataType, tuple[str, int]] = {
    DataType.INT16: (">h", 1),
    DataType.UINT16: (">H", 1),
    DataType.INT32: (">i", 2),
    DataType.UINT32: (">I", 2),
    DataType.FLOAT32: (">f", 2),
    DataType.FLOAT64: (">d", 4),
}

_BYTE_SIZE: dict[DataType, int] = {
    DataType.BOOL: 1,
    DataType.INT16: 2,
    DataType.UINT16: 2,
    DataType.INT32: 4,
    DataType.UINT32: 4,
    DataType.FLOAT32: 4,
    DataType.FLOAT64: 8,
}


def register_span(data_type: DataType) -> int:
    """Number of 16-bit registers occupied by a value."""
    if data_type is DataType.BOOL:
        return 1
    return _REGISTER_FMT[data_type][1]


def byte_size(data_type: DataType) -> int:
    return _BYTE_SIZE[data_type]


def coerce(value: Any, data_type: DataType) -> Any:
    """Best-effort conversion of an incoming JSON value to the tag's Python type."""
    if data_type is DataType.BOOL:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "on", "yes"}
        return bool(value)
    if data_type in (
        DataType.INT16,
        DataType.UINT16,
        DataType.INT32,
        DataType.UINT32,
    ):
        return int(round(float(value)))
    return float(value)


def _swap_words(data: bytes) -> bytes:
    """Reverse 16-bit word order without touching byte order inside a word."""
    words = [data[i : i + 2] for i in range(0, len(data), 2)]
    return b"".join(reversed(words))


def to_registers(value: Any, data_type: DataType, word_order: WordOrder) -> list[int]:
    value = coerce(value, data_type)
    if data_type is DataType.BOOL:
        return [1 if value else 0]
    fmt, count = _REGISTER_FMT[data_type]
    raw = struct.pack(fmt, value)
    words = [int.from_bytes(raw[i : i + 2], "big") for i in range(0, count * 2, 2)]
    if word_order is WordOrder.LITTLE:
        words.reverse()
    return words


def from_registers(words: list[int], data_type: DataType, word_order: WordOrder) -> Any:
    if data_type is DataType.BOOL:
        return bool(words[0]) if words else False
    fmt, count = _REGISTER_FMT[data_type]
    if len(words) < count:
        raise ValueError(f"need {count} registers, got {len(words)}")
    ordered = list(words[:count])
    if word_order is WordOrder.LITTLE:
        ordered.reverse()
    raw = b"".join(int(word & 0xFFFF).to_bytes(2, "big") for word in ordered)
    return struct.unpack(fmt, raw)[0]


def to_bytes(value: Any, data_type: DataType, word_order: WordOrder) -> bytes:
    value = coerce(value, data_type)
    if data_type is DataType.BOOL:
        return b"\x01" if value else b"\x00"
    fmt, _ = _REGISTER_FMT[data_type]
    raw = struct.pack(fmt, value)
    if word_order is WordOrder.LITTLE:
        raw = _swap_words(raw)
    return raw


def from_bytes(raw: bytes, data_type: DataType, word_order: WordOrder) -> Any:
    if data_type is DataType.BOOL:
        return raw[0] != 0
    fmt, count = _REGISTER_FMT[data_type]
    need = count * 2
    if len(raw) < need:
        raise ValueError(f"need {need} bytes, got {len(raw)}")
    data = bytes(raw[:need])
    if word_order is WordOrder.LITTLE:
        data = _swap_words(data)
    return struct.unpack(fmt, data)[0]
