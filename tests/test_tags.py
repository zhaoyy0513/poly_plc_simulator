from poly_contracts import DataType, Protocol, TagDef, WordOrder
from poly_core import validate_tags

# Modbus instances have no tag table, so the validator only has rules for the
# protocols that do: S7 (byte-addressed areas) and OPC UA (name = NodeId).


def test_s7_overlap_rejected() -> None:
    tags = [
        TagDef(name="a", data_type=DataType.FLOAT32, area="db", db_number=1, address=0),
        TagDef(name="b", data_type=DataType.FLOAT32, area="db", db_number=1, address=2),
    ]
    errors = validate_tags(Protocol.S7, tags)
    assert any("重叠" in error for error in errors)


def test_s7_bit_only_for_bool() -> None:
    tags = [TagDef(name="c", data_type=DataType.FLOAT32, area="m", address=0, bit=0)]
    assert any("bit" in error for error in validate_tags(Protocol.S7, tags))


def test_s7_requires_bit_for_bool() -> None:
    tags = [TagDef(name="r", data_type=DataType.BOOL, area="m", address=0)]
    assert any("bit" in error for error in validate_tags(Protocol.S7, tags))


def test_s7_db_requires_number() -> None:
    tags = [TagDef(name="t", data_type=DataType.FLOAT32, area="db", address=0)]
    assert any("db_number" in error for error in validate_tags(Protocol.S7, tags))


def test_s7_rejects_little_word_order() -> None:
    tags = [
        TagDef(
            name="t",
            data_type=DataType.FLOAT32,
            area="db",
            db_number=1,
            address=0,
            word_order=WordOrder.LITTLE,
        )
    ]
    assert validate_tags(Protocol.S7, tags)


def test_duplicate_names_case_insensitive() -> None:
    tags = [
        TagDef(name="Temp", data_type=DataType.UINT16, area="db", db_number=1, address=0),
        TagDef(name="temp", data_type=DataType.UINT16, area="db", db_number=1, address=4),
    ]
    assert any("重复" in error for error in validate_tags(Protocol.S7, tags))


def test_valid_table_passes() -> None:
    tags = [
        TagDef(name="t", data_type=DataType.FLOAT32, area="db", db_number=1, address=0),
        TagDef(name="c", data_type=DataType.UINT16, area="db", db_number=1, address=4),
        TagDef(name="run", data_type=DataType.BOOL, area="m", address=0, bit=0),
    ]
    assert validate_tags(Protocol.S7, tags) == []
