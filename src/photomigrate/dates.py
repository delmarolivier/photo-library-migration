"""Comparing capture times when some carry a UTC offset and others are bare local time.

EXIF often records only local wall-clock time (no offset), while sidecars and QuickTime record
UTC instants. A local time and an instant can only differ by a whole time-zone offset: a
multiple of 15 minutes, at most 14 hours. Anything else means they disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_MAX_OFFSET = timedelta(hours=14)
_QUARTER_HOUR = timedelta(minutes=15)


def moment_gap(a: datetime, b: datetime) -> timedelta:
    """How far apart ``a`` and ``b`` are, allowing a whole time-zone offset between a local
    time and an instant. Beyond 14 hours apart, the plain difference is returned."""
    if (a.tzinfo is None) == (b.tzinfo is None):
        return abs(a - b)
    local, instant = (a, b) if a.tzinfo is None else (b, a)
    delta = abs(local - instant.astimezone(UTC).replace(tzinfo=None))
    remainder = delta % _QUARTER_HOUR
    nearest = min(remainder, _QUARTER_HOUR - remainder)
    return delta if delta - nearest > _MAX_OFFSET else nearest


def same_moment(a: datetime, b: datetime, tolerance: timedelta) -> bool:
    """Whether ``a`` and ``b`` can be the same moment, within ``tolerance``."""
    return moment_gap(a, b) <= tolerance
