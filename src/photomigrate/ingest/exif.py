"""exiftool, behind a small typed wrapper: read embedded metadata, write planned metadata.

Writes never touch the source. ``write`` asks exiftool for a *new* file (``-o``), so
originals, whether Takeout zip entries or extracted files, are only ever read. Every path is
announced to the safety guard first, because PyExifTool sends paths over stdin where audit
hooks cannot see them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import exiftool
from exiftool.exceptions import ExifToolException

from photomigrate.safety import announce_path

EXIF_PHOTOS = frozenset(
    {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".dng", ".webp"}
)
XMP_PHOTOS = frozenset({".gif"})
QUICKTIME_VIDEOS = frozenset({".mov", ".mp4", ".m4v", ".3gp"})

_READ_TAGS = [
    "DateTimeOriginal", "OffsetTimeOriginal", "CreateDate", "CreationDate",
    "GPSLatitude#", "GPSLongitude#", "GPSAltitude#",
    "Description", "ImageDescription", "Caption-Abstract",
    "ImageSize#", "Duration#",
]  # fmt: skip
_EXIF_TIME = re.compile(
    r"^(?P<date>\d{4}:\d\d:\d\d) (?P<time>\d\d:\d\d:\d\d)"
    r"(?P<fraction>\.\d+)?(?P<offset>[+-]\d\d:\d\d|Z)?$"
)


_QUICKTIME_EPOCH_YEAR = 1904


class MetadataError(RuntimeError):
    """exiftool could not read or write a file."""


def can_write(name: str) -> bool:
    suffix = Path(name).suffix.lower()
    return suffix in EXIF_PHOTOS | XMP_PHOTOS | QUICKTIME_VIDEOS


@dataclass(frozen=True)
class Embedded:
    """Metadata already in a file. ``taken`` is timezone-aware only if the file says so."""

    taken: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    description: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None


@dataclass(frozen=True)
class MetadataWrite:
    """What ingest decided to write into a working copy. Unset fields are left alone."""

    taken: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if self.taken is not None and self.taken.tzinfo is None:
            raise ValueError("a capture time to write needs a timezone offset")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude go together")
        if self.altitude is not None and self.latitude is None:
            raise ValueError("an altitude needs a latitude and longitude")

    def __bool__(self) -> bool:
        return any(value is not None for value in self.to_dict().values())

    def to_dict(self) -> dict[str, object]:
        return {
            "taken": self.taken.isoformat() if self.taken else None,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude": self.altitude,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetadataWrite:
        taken = data.get("taken")
        return cls(
            taken=datetime.fromisoformat(taken) if taken else None,
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            altitude=data.get("altitude"),
            description=data.get("description"),
        )


def parse_exif_time(value: object) -> datetime | None:
    """``2019:07:01 12:34:56[.123][+02:00]`` to a datetime; zero or junk dates to ``None``."""
    found = _EXIF_TIME.match(str(value).strip())
    if not found or found["date"].startswith("0000"):
        return None
    offset = found["offset"] or ""
    text = f"{found['date'].replace(':', '-')}T{found['time']}{found['fraction'] or ''}"
    try:
        return datetime.fromisoformat(text + ("+00:00" if offset == "Z" else offset))
    except ValueError:
        return None


def _quicktime_time(value: object) -> datetime | None:
    """QuickTime dates count seconds from 1904; some exiftool versions show unset ones as 1904."""
    when = parse_exif_time(value)
    if when is None:
        return None
    utc_year = when.year if when.tzinfo is None else when.astimezone(UTC).year
    return None if min(when.year, utc_year) <= _QUICKTIME_EPOCH_YEAR else when


def _local(when: datetime) -> str:
    return f"{when:%Y:%m:%d %H:%M:%S}"


def _offset(when: datetime) -> str:
    text = when.strftime("%z")
    return f"{text[:3]}:{text[3:]}"


def _tags(suffix: str, plan: MetadataWrite) -> dict[str, object]:
    tags: dict[str, object] = {}
    lat, lon, alt = plan.latitude, plan.longitude, plan.altitude
    if suffix in QUICKTIME_VIDEOS:
        if plan.taken:
            stamp = _local(plan.taken) + _offset(plan.taken)
            tags |= {"QuickTime:CreateDate": stamp, "Keys:CreationDate": stamp}
        if lat is not None and lon is not None:
            tags["Keys:GPSCoordinates#"] = f"{lat} {lon} {alt or 0}"
        if plan.description is not None:
            tags["Keys:Description"] = plan.description
        return tags
    if suffix in XMP_PHOTOS:
        if plan.taken:
            tags["XMP-exif:DateTimeOriginal"] = _local(plan.taken) + _offset(plan.taken)
        if lat is not None and lon is not None:
            tags |= {"XMP-exif:GPSLatitude#": lat, "XMP-exif:GPSLongitude#": lon}
            if alt is not None:
                tags |= {
                    "XMP-exif:GPSAltitude#": abs(alt),
                    "XMP-exif:GPSAltitudeRef#": int(alt < 0),
                }
    else:
        if plan.taken:
            tags |= {
                "EXIF:DateTimeOriginal": _local(plan.taken),
                "EXIF:OffsetTimeOriginal": _offset(plan.taken),
            }
        if lat is not None and lon is not None:
            tags |= {
                "EXIF:GPSLatitude#": abs(lat),
                "EXIF:GPSLatitudeRef": "N" if lat >= 0 else "S",
                "EXIF:GPSLongitude#": abs(lon),
                "EXIF:GPSLongitudeRef": "E" if lon >= 0 else "W",
            }
            if alt is not None:
                tags |= {"EXIF:GPSAltitude#": abs(alt), "EXIF:GPSAltitudeRef#": int(alt < 0)}
    if plan.description is not None:
        tags["XMP-dc:Description"] = plan.description
    return tags


def _first(found: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if found.get(key) not in (None, ""):
            return found[key]
    return None


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def embedded_from_tags(found: dict[str, Any]) -> Embedded:
    """Turn exiftool's ``-G1`` tag dict into an ``Embedded``."""
    exif_time = parse_exif_time(found.get("ExifIFD:DateTimeOriginal"))
    offset = found.get("ExifIFD:OffsetTimeOriginal")
    if exif_time is not None and exif_time.tzinfo is None and offset:
        exif_time = parse_exif_time(f"{found['ExifIFD:DateTimeOriginal']}{offset}")
    taken = (
        exif_time
        or parse_exif_time(found.get("XMP-exif:DateTimeOriginal"))
        or _quicktime_time(found.get("Keys:CreationDate"))
        or _quicktime_time(found.get("QuickTime:CreateDate"))
    )
    width = height = None
    size = str(found.get("Composite:ImageSize") or "")
    dimensions = re.findall(r"\d+", size)
    if len(dimensions) == 2:
        width, height = int(dimensions[0]), int(dimensions[1])
    description = _first(
        found,
        "XMP-dc:Description",
        "Keys:Description",
        "IFD0:ImageDescription",
        "IPTC:Caption-Abstract",
    )
    return Embedded(
        taken=taken,
        latitude=_number(_first(found, "Composite:GPSLatitude", "XMP-exif:GPSLatitude")),
        longitude=_number(_first(found, "Composite:GPSLongitude", "XMP-exif:GPSLongitude")),
        altitude=_number(_first(found, "Composite:GPSAltitude", "XMP-exif:GPSAltitude")),
        description=None if description is None else str(description),
        width=width,
        height=height,
        duration=_number(found.get("QuickTime:Duration")),
    )


class ExifTool:
    """One long-running exiftool process. Use as a context manager."""

    def __init__(self) -> None:
        self._tool = exiftool.ExifToolHelper(
            common_args=["-G1", "-api", "QuickTimeUTC=1"], check_tag_names=False, encoding="utf-8"
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._tool.running:
            self._tool.terminate()

    def read(self, path: Path) -> Embedded:
        announce_path(path)
        try:
            (found,) = self._tool.get_tags([str(path)], tags=_READ_TAGS)
        except ExifToolException as exc:
            raise MetadataError(f"exiftool could not read {path}: {exc}") from exc
        return embedded_from_tags(found)

    def write(self, source: Path, dest: Path, plan: MetadataWrite) -> None:
        """Write ``plan`` into a new file ``dest`` built from ``source`` (left untouched)."""
        if not plan:
            raise ValueError("nothing to write")
        if not can_write(source.name):
            raise MetadataError(f"exiftool cannot write {source.suffix} files: {source}")
        if dest.exists():
            raise MetadataError(f"working copy already exists: {dest}")
        announce_path(source)
        announce_path(dest)
        try:
            self._tool.set_tags(
                [str(source)], tags=_tags(source.suffix.lower(), plan), params=["-o", str(dest)]
            )
        except ExifToolException as exc:
            raise MetadataError(f"exiftool could not write {dest}: {exc}") from exc
