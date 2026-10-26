"""OPC UA vocabulary shared by the server adapter and the link client.

Two things both sides need and neither owns:

* the mapping from our `DataType` to the wire variant, and
* the rule that turns a tag into a NodeId.

Keeping them here means the server and the client can never drift apart on
either, which matters because a mismatch shows up as `BadTypeMismatch` on a
live device rather than as a failing test.
"""

from __future__ import annotations

from poly_contracts import DataType, TagDef

from asyncua import ua

VARIANT: dict[DataType, ua.VariantType] = {
    DataType.BOOL: ua.VariantType.Boolean,
    DataType.INT16: ua.VariantType.Int16,
    DataType.UINT16: ua.VariantType.UInt16,
    DataType.INT32: ua.VariantType.Int32,
    DataType.UINT32: ua.VariantType.UInt32,
    DataType.FLOAT32: ua.VariantType.Float,
    DataType.FLOAT64: ua.VariantType.Double,
}


def node_id_for(tag: TagDef, namespace_index: int) -> str:
    """The NodeId this tag addresses on the far side.

    Our own server names every variable `ns=<index>;s=<tag name>`, so a tag
    with no explicit NodeId links to a simulator with nothing to configure. A
    third-party server is free to use numeric identifiers or its own namespace
    index; that is what `TagDef.node_id` is for.
    """
    explicit = (tag.node_id or "").strip()
    if explicit:
        return explicit
    return f"ns={namespace_index};s={tag.name}"
