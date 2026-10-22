from poly_contracts import DataType, WordOrder
from poly_core import byte_size, from_bytes, from_registers, register_span, to_bytes, to_registers


def test_float32_word_order() -> None:
    big = to_registers(1.5, DataType.FLOAT32, WordOrder.BIG)
    little = to_registers(1.5, DataType.FLOAT32, WordOrder.LITTLE)
    assert big != little
    assert from_registers(big, DataType.FLOAT32, WordOrder.BIG) == 1.5
    assert from_registers(little, DataType.FLOAT32, WordOrder.LITTLE) == 1.5


def test_roundtrip_all_types() -> None:
    samples = {
        DataType.INT16: -1234,
        DataType.UINT16: 65535,
        DataType.INT32: -70000,
        DataType.UINT32: 4_000_000_000,
        DataType.FLOAT32: 3.25,
        DataType.FLOAT64: -9.5,
    }
    for dtype, value in samples.items():
        for order in (WordOrder.BIG, WordOrder.LITTLE):
            words = to_registers(value, dtype, order)
            assert len(words) == register_span(dtype)
            assert from_registers(words, dtype, order) == value
            raw = to_bytes(value, dtype, order)
            assert len(raw) == byte_size(dtype)
            assert from_bytes(raw, dtype, order) == value


def test_bool_and_int_width() -> None:
    assert to_registers(True, DataType.BOOL, WordOrder.BIG) == [1]
    assert register_span(DataType.FLOAT64) == 4
    assert byte_size(DataType.FLOAT64) == 8
