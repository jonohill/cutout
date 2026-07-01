from __future__ import annotations

import re

# systemd-style time-span units, in seconds. Case matters for the single-letter
# forms: lowercase ``m`` is minutes, capital ``M`` is months — this is exactly
# how systemd disambiguates the two. Month and year use systemd's fixed
# approximations (30.44 and 365.25 days); they never mean calendar arithmetic.
_UNITS = {
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
    "M": 2629800, "month": 2629800, "months": 2629800,
    "y": 31557600, "year": 31557600, "years": 31557600,
}

# Whole string must be one or more "<number><unit>" components, whitespace
# optional between them (e.g. "60m", "1h30min", "2d 4h").
_SHAPE_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)?\s*[A-Za-z]+\s*)+$")
_COMPONENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-z]+)")


def parse_duration(value: str) -> int:
    """Parse a systemd-style time span into a whole number of seconds.

    Accepts single letters and their long-form aliases, case-sensitive for the
    single letters (``m``/``min`` = minutes, ``M``/``month`` = months):
    ``s/sec``, ``m/min``, ``h/hr``, ``d/day``, ``w/week``, ``M/month``,
    ``y/year``. Components may be concatenated or space-separated
    (``"1h30min"``, ``"2d 4h"``). ``"0"`` means disabled.

    Raises ``ValueError`` if ``value`` is not a valid duration.
    """
    text = value.strip()
    if text == "0":
        return 0
    if not _SHAPE_RE.match(text):
        raise ValueError(f"invalid duration: {value!r}")
    total = 0.0
    for qty, unit in _COMPONENT_RE.findall(text):
        try:
            factor = _UNITS[unit]
        except KeyError:
            raise ValueError(f"invalid duration unit {unit!r} in {value!r}") from None
        total += float(qty) * factor
    return round(total)
