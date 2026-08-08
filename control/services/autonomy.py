from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


class AutonomyMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    LOW_RISK = "LOW_RISK"
    FULL = "FULL"


@dataclass(frozen=True)
class AutonomyDecision:
    mode: AutonomyMode
    may_evaluate: bool
    may_acquire: bool
    reason_codes: tuple[str, ...]


def current_mode() -> AutonomyMode:
    try:
        return AutonomyMode(os.getenv("AUTONOMOUS_MODE", "OFF").strip().upper())
    except ValueError:
        return AutonomyMode.OFF


def acquisition_autonomy(*, switch_enabled: bool) -> AutonomyDecision:
    raw = os.getenv("AUTONOMOUS_MODE", "OFF").strip().upper()
    mode = current_mode()
    reasons: list[str] = []
    if raw not in {item.value for item in AutonomyMode}:
        reasons.append("AUTONOMY_MODE_INVALID")
    if mode == AutonomyMode.OFF:
        reasons.append("AUTONOMY_OFF")
    elif mode == AutonomyMode.SHADOW:
        reasons.append("AUTONOMY_SHADOW_ONLY")
    if not switch_enabled:
        reasons.append("ACQUISITION_SWITCH_DISABLED")
    return AutonomyDecision(
        mode=mode,
        may_evaluate=True,
        may_acquire=mode in {AutonomyMode.LOW_RISK, AutonomyMode.FULL} and switch_enabled and not reasons,
        reason_codes=tuple(reasons),
    )
