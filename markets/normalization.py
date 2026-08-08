from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def first_value(raw: dict[str, Any], *keys: str, default=None):
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return default


def decimal_reward(raw: dict[str, Any], *keys: str, cents: bool = False) -> Decimal:
    value = first_value(raw, *keys, default=0)
    if isinstance(value, dict):
        value = first_value(value, "amount", "value", "cents", default=0)
    try:
        reward = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return reward / Decimal("100") if cents else reward


def list_rows(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    data = payload.get("data")
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
