"""Creation defaults must not invent device data."""

from __future__ import annotations

from poly_contracts import Protocol, ServerCreate


def test_sample_tags_default_is_false() -> None:
    body = ServerCreate(name="s7-empty", protocol=Protocol.S7, port=15102)
    assert body.sample_tags is False


def test_explicit_sample_tags_true_still_works() -> None:
    body = ServerCreate(name="s7-demo", protocol=Protocol.S7, port=15102, sample_tags=True)
    assert body.sample_tags is True
