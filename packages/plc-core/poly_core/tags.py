"""Tag-table validation.

Each protocol addresses memory differently, so the rules live here instead of
inside the adapters: an invalid tag must be rejected before a runtime starts.

Modbus is intentionally absent: those instances have no tag table at all, their
address space is four raw areas that the connected client interprets.
"""

from __future__ import annotations

from poly_contracts import DataType, Protocol, TagDef

from .codec import byte_size

S7_AREAS = ("db", "m", "i", "q")


def _area_key(protocol: Protocol, tag: TagDef) -> tuple[str, int | None]:
    if protocol is Protocol.S7:
        return tag.area, tag.db_number if tag.area == "db" else None
    return tag.area, None


def validate_tags(protocol: Protocol, tags: list[TagDef]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
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
