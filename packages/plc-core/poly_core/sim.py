"""Tiny value generator used when a tag should move on its own.

Kept deliberately simple and allocation-free per tick: the runtime ticks every
200 ms, so this must stay cheap for hundreds of tags.
"""

from __future__ import annotations

import math
import random
from typing import Any

from poly_contracts import DataType, SimMode, TagDef

from .codec import coerce


def _phase(name: str) -> float:
    """Deterministic phase offset so tags do not all animate in lockstep."""
    return (hash(name) % 1000) / 1000.0 * 2 * math.pi


def next_value(tag: TagDef, elapsed: float, previous: Any) -> Any:
    spec = tag.sim
    if spec.mode is SimMode.STATIC:
        return None

    if tag.data_type is DataType.BOOL:
        if spec.mode is SimMode.TOGGLE:
            period = max(spec.period_ms / 1000.0, 0.2)
            return bool(int(elapsed / period) % 2)
        return bool(random.getrandbits(1))

    low, high = spec.min, spec.max
    if high < low:
        low, high = high, low

    if spec.mode is SimMode.SINE:
        period = max(spec.period_ms / 1000.0, 0.2)
        mid = (low + high) / 2
        amp = (high - low) / 2
        return mid + amp * math.sin(2 * math.pi * elapsed / period + _phase(tag.name))

    if spec.mode is SimMode.RANDOM:
        return random.uniform(low, high)

    if spec.mode is SimMode.RAMP:
        base = float(previous) if previous is not None else low
        step = abs(spec.step) or 1.0
        value = base + step
        if value > high:
            value = low
        return value

    return None


def seed_value(tag: TagDef) -> Any:
    """Initial value before the first tick."""
    if tag.data_type is DataType.BOOL:
        return False
    spec = tag.sim
    if spec.mode is SimMode.STATIC:
        return coerce(0, tag.data_type)
    return coerce((spec.min + spec.max) / 2, tag.data_type)
