"""Tag-table validation.

Each protocol addresses memory differently, so the rules live here instead of
inside the adapters: an invalid tag must be rejected before a runtime starts.

Modbus is intentionally absent: those instances have no tag table at all, their
address space is four raw areas that the connected client interprets.
"""

from __future__ import annotations

import re

from poly_contracts import DataType, Protocol, TagDef

from .codec import byte_size

S7_AREAS = ("db", "m", "i", "q")

# Deliberately loose. asyncua also understands `nsu=<uri>;` and numeric/null
# identifiers, and getting the grammar slightly wrong here would reject a
# perfectly good NodeId. The point is only to catch what asyncua reports as
# `UaStringParsingError` at read time - a typo the user should see before the
# instance is saved, not on the first page refresh.
_OPCUA_NODE_ID = re.compile(r"^(?:ns=\d+;|nsu=[^;]+;)?[sibg]=.+$", re.IGNORECASE)


def _area_key(protocol: Protocol, tag: TagDef) -> tuple[str, int | None]:
    if protocol is Protocol.S7:
        return tag.area, tag.db_number if tag.area == "db" else None
    return tag.area, None


def validate_tags(protocol: Protocol, tags: list[TagDef]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    node_ids: dict[str, str] = {}
    spans: dict[tuple[str, int | None], list[tuple[int, int, str]]] = {}

    for tag in tags:
        key = tag.name.lower()
        if key in seen:
            errors.append(f"变量名重复: {tag.name}")
        seen.add(key)

        if protocol is Protocol.S7:
            if tag.area not in S7_AREAS:
                errors.append(f"{tag.name}: S7 区域必须是 {S7_AREAS}")
                continue
            if tag.area == "db" and tag.db_number is None:
                errors.append(f"{tag.name}: area=db 时必须填写 db_number")
            if tag.area != "db" and tag.db_number is not None:
                errors.append(f"{tag.name}: 只有 area=db 才能填 db_number")
            if tag.bit is not None and tag.data_type is not DataType.BOOL:
                errors.append(f"{tag.name}: bit 只能用于 bool")
            if tag.data_type is DataType.BOOL and tag.bit is None:
                errors.append(f"{tag.name}: S7 的 bool 必须指定位号 bit(0-7)")
            span = byte_size(tag.data_type)
            if tag.address + span > 65536:
                errors.append(f"{tag.name}: 地址越界")
            if tag.word_order.value == "little":
                errors.append(f"{tag.name}: S7 固定大端，不支持 word_order=little")
        else:  # OPC UA: names are the address, memory fields are irrelevant
            span = 0
            if tag.node_id:
                if not _OPCUA_NODE_ID.match(tag.node_id.strip()):
                    errors.append(
                        f"{tag.name}: NodeId 格式不对（{tag.node_id!r}）；应形如 ns=2;s=temperature"
                        f" 或 ns=3;i=1001，也可以留空表示 ns=<链接的命名空间索引>;s={tag.name}"
                    )
                else:
                    node_key = tag.node_id.strip().lower()
                    if node_key in node_ids:
                        errors.append(
                            f"NodeId 重复: {tag.name} 与 {node_ids[node_key]} 都指向 {tag.node_id}"
                        )
                    else:
                        node_ids[node_key] = tag.name
            elif protocol is Protocol.OPCUA and tag.area not in ("opcua", ""):
                errors.append(
                    f"{tag.name}: OPC UA 变量不按区域+偏移寻址，area 应为 opcua（当前 {tag.area!r}）"
                )

        if span:
            spans.setdefault(_area_key(protocol, tag), []).append(
                (tag.address, tag.address + span, tag.name)
            )

    for area_key, ranges in spans.items():
        ranges.sort()
        for previous, current in zip(ranges, ranges[1:]):
            if current[0] < previous[1]:
                errors.append(
                    f"地址重叠: {previous[2]} 与 {current[2]} 在同一区域 {area_key[0]}"
                    + (f"/DB{area_key[1]}" if area_key[1] else "")
                )

    return errors


def s7_area_sizes(tags: list[TagDef]) -> dict[tuple[str, int], int]:
    """Bytes needed per (area, db_number) buffer."""
    sizes: dict[tuple[str, int], int] = {}
    for tag in tags:
        key = (tag.area, tag.db_number or 0)
        needed = tag.address + byte_size(tag.data_type) + 8
        sizes[key] = max(sizes.get(key, 128), needed)
    if not sizes:
        sizes[("db", 1)] = 256
    return sizes
