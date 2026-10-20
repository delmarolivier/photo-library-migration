"""Deciding what ingest writes into a working copy, and which date candidates it records.

The rules, in order of trust:

* **Capture date.** A real date in the file wins. A sidecar date fills the gap only if it is
  plausible: not a placeholder (1970-01-01, 1980-01-01, 2000-01-01), not after the upload
  (beyond a day), and not equal to the upload time (Google's fallback for unknown dates).
  The sidecar holds UTC only, so the date is written in the home time zone and flagged
  ``offset-assumed``. Disagreements become ``date-conflict`` for the date audit (M3-03), and
  nothing is overwritten.
* **GPS and caption.** These come from the sidecar when the file has none, or a different
  caption.

Every decision leaves flags behind, so the date audit and the review can explain themselves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from photomigrate.catalogue import DateSource
from photomigrate.ingest.exif import Embedded, MetadataWrite
from photomigrate.ingest.names import Kind, date_from_filename
from photomigrate.ingest.sidecars import SidecarData

PLACEHOLDER_DAYS = frozenset({date(1970, 1, 1), date(1980, 1, 1), date(2000, 1, 1)})
_SAME_INSTANT = timedelta(seconds=2)
_MAX_OFFSET = timedelta(hours=14)
_QUARTER_HOUR = 900


@dataclass(frozen=True)
class Decision:
    plan: MetadataWrite
    flags: frozenset[str]


def _day(when: datetime) -> date:
    return when.date() if when.tzinfo is None else when.astimezone(UTC).date()


def is_placeholder(when: datetime) -> bool:
    return _day(when) in PLACEHOLDER_DAYS


def _conflict(embedded: datetime, sidecar: datetime) -> bool:
    if embedded.tzinfo is not None:
        return abs(embedded - sidecar) > _SAME_INSTANT
    # Local time with an unknown offset: equal if a whole number of quarter hours apart.
    delta = abs(embedded - sidecar.astimezone(UTC).replace(tzinfo=None))
    if delta > _MAX_OFFSET:
        return True
    remainder = delta.total_seconds() % _QUARTER_HOUR
    return min(remainder, _QUARTER_HOUR - remainder) > _SAME_INSTANT.total_seconds()


def _sidecar_date(sidecar: SidecarData | None, flags: set[str]) -> datetime | None:
    if sidecar is None or sidecar.taken is None:
        return None
    taken, created = sidecar.taken, sidecar.created
    if is_placeholder(taken):
        flags.add("sidecar-date-placeholder")
    elif created is not None and taken > created + timedelta(days=1):
        flags.add("sidecar-date-after-upload")
    elif created is not None and taken == created:
        flags.add("sidecar-date-is-upload")
    else:
        return taken
    return None


def decide(
    *, sidecar: SidecarData | None, embedded: Embedded, zone: tzinfo, writable: bool
) -> Decision:
    flags: set[str] = set() if sidecar else {"no-sidecar"}
    taken = None
    from_sidecar = _sidecar_date(sidecar, flags)
    file_date = embedded.taken
    if file_date is not None and is_placeholder(file_date):
        flags.add("embedded-date-placeholder")
        file_date = None
    if file_date is not None:
        if from_sidecar is not None and _conflict(file_date, from_sidecar):
            flags.add("date-conflict")
    elif from_sidecar is not None:
        taken = from_sidecar.astimezone(zone)
        flags |= {"date-from-sidecar", "offset-assumed"}
    else:
        flags.add("date-unknown")

    latitude = longitude = altitude = None
    if embedded.latitude is None and sidecar is not None and sidecar.geo is not None:
        latitude, longitude, altitude = sidecar.geo
        flags.add("gps-from-sidecar")
    elif embedded.latitude is None:
        flags.add("no-gps")

    description = None
    if sidecar is not None and sidecar.description and sidecar.description != embedded.description:
        description = sidecar.description
        flags.add("description-from-sidecar")

    plan = MetadataWrite(taken, latitude, longitude, altitude, description)
    if plan and not writable:
        flags.add("metadata-not-writable")
    return Decision(plan, frozenset(flags))


def date_candidates(
    *,
    kind: Kind,
    embedded: Embedded,
    sidecar: SidecarData | None,
    name: str,
    modified: datetime,
) -> dict[DateSource, datetime]:
    """Every capture-date clue for the date audit, keyed by where it came from."""
    found: dict[DateSource, datetime] = {}
    if embedded.taken is not None:
        found["exif" if kind == "photo" else "quicktime"] = embedded.taken
    if sidecar is not None and sidecar.taken is not None:
        found["sidecar-taken"] = sidecar.taken
    if sidecar is not None and sidecar.created is not None:
        found["sidecar-created"] = sidecar.created
    in_name = date_from_filename(name)
    if in_name is not None:
        found["filename"] = in_name
    found["mtime"] = modified
    return found


def _localtime_target() -> str | None:
    try:
        return os.readlink("/etc/localtime")
    except OSError:
        return None


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_zone() -> ZoneInfo:
    """This machine's IANA time zone: ``$TZ``, else ``/etc/localtime``, else UTC."""
    target = _localtime_target() or ""
    linked = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else None
    return _zone(os.environ.get("TZ")) or _zone(linked) or ZoneInfo("UTC")
