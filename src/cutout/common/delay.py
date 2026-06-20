from __future__ import annotations

import re
from datetime import timedelta

_DELAY_RE = re.compile(r"^(\d+)([dwmy])$")
_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def parse_delay(value: str) -> timedelta:
    """Parse a delay string like ``"2y"`` into a ``timedelta``.

    Raises ``ValueError`` if ``value`` is not a valid delay.
    """
    match = _DELAY_RE.match(value)
    if not match:
        raise ValueError(f"invalid delay: {value!r}")
    n = int(match.group(1))
    return timedelta(days=n * _UNIT_DAYS[match.group(2)])
