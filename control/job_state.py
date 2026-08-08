class InvalidJobTransition(ValueError):
    pass


TERMINAL_STATES = {"SETTLED", "FAILED"}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERED": {"EXPECTED", "FAILED"},
    "EXPECTED": {"CLAIMED", "AWARDED", "FAILED"},
    "CLAIMED": {"EXECUTING", "FAILED"},
    "AWARDED": {"EXECUTING", "FAILED"},
    "EXECUTING": {"SUBMITTED", "FAILED"},
    "SUBMITTED": {"ACCEPTED", "EXECUTING", "FAILED"},
    "ACCEPTED": {"PAYOUT_PENDING", "SETTLED", "FAILED"},
    "PAYOUT_PENDING": {"SETTLED", "FAILED"},
    "SETTLED": set(),
    "FAILED": set(),
}


def assert_transition(current: str, target: str) -> None:
    if current == target:
        return
    allowed = ALLOWED_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise InvalidJobTransition(f"invalid job transition: {current} -> {target}")
