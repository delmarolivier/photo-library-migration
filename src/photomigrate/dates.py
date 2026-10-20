"""Comparing capture times when some carry a UTC offset and others are bare local time.

EXIF often records only local wall-clock time (no offset), while sidecars and QuickTime record
UTC instants. A local time and an instant can only differ by a whole time-zone offset: a
multiple of 15 minutes, at most 14 hours. Anything else means they disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_MAX_OFFSET = timedelta(hours=14)
_QUARTER_HOUR = timedelta(minutes=15)


def same_moment(a: datetime, b: datetime, tolerance: timedelta) -> bool:
    """Whether ``a`` and ``b`` can be the same moment, within ``tolerance``."""
    if (a.tzinfo is None) == (b.tzinfo is None):
        return abs(a - b) <= tolerance
    local, instant = (a, b) if a.tzinfo is None else (b, a)
    delta = abs(local - instant.astimezone(UTC).replace(tzinfo=None))
    if delta > _MAX_OFFSET + tolerance:
        return False
    remainder = delta % _QUARTER_HOUR
    return min(remainder, _QUARTER_HOUR - remainder) <= tolerance
