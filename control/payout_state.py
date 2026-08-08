class InvalidPayoutTransition(ValueError):
    pass


ALLOWED_PAYOUT_TRANSITIONS = {
    None: {"EARNED"},
    "EARNED": {"PAYOUT_PENDING", "SETTLED", "REVERSED"},
    "PAYOUT_PENDING": {"SETTLED", "REVERSED"},
    "SETTLED": {"REVERSED"},
    "REVERSED": set(),
}


def assert_payout_transition(current: str | None, target: str) -> None:
    if current == target:
        return
    allowed = ALLOWED_PAYOUT_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise InvalidPayoutTransition(f"invalid payout transition: {current} -> {target}")
