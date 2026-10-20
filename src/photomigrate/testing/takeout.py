"""Synthetic Google Takeout exports for tests (M1-02).

The naming rules follow docs/TAKEOUT_FORMAT.md. Rules seen in Delmar's real sample:
``.supplemental-metadata.json`` sidecars, ``(N)`` moving to just before ``.json``, and
``-edited`` copies without their own sidecar. Rules modelled on public reports (the
google-photos-takeout-helper project) and not in the sample: legacy ``.json`` sidecars,
47-character media names, 51-character sidecar names, and localised edit suffixes. If a real
export disagrees, fix the model here and in the doc.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from photomigrate.testing.media import utc

SidecarStyle = Literal["supplemental", "legacy"]
Locale = Literal["en_GB", "en_US"]
Origin = Literal["desktop", "mobile", "auto"]

MEDIA_NAME_LIMIT = 47
SIDECAR_NAME_LIMIT = 51
SUPPLEMENTAL_SUFFIX = ".supplemental-metadata"

EDITED_SUFFIXES = {
    "en": "-edited",
    "fr": "-modifié",
    "de": "-bearbeitet",
    "it": "-modificato",
    "nl": "-bewerkt",
    "pl": "-edytowane",
    "ja": "-編集済み",
}


def _split(name: str) -> tuple[str, str]:
    suffix = PurePosixPath(name).suffix
    return name[: len(name) - len(suffix)], suffix


def truncate_media_name(name: str) -> str:
    """Cut a media file name to 47 characters, keeping its extension."""
    stem, ext = _split(name)
    return stem[: MEDIA_NAME_LIMIT - len(ext)] + ext


def duplicate_name(name: str, index: int) -> str:
    """``IMG_1234.JPG`` with index 1 becomes ``IMG_1234(1).JPG``."""
    if not index:
        return name
    stem, ext = _split(name)
    return f"{stem}({index}){ext}"


def edited_name(
    name: str, *, lang: str = "en", extension: str | None = None, index: int = 0
) -> str:
    """Name of the edited copy of the media file ``name`` (its on-disk name, without index)."""
    stem, ext = _split(name)
    edited = truncate_media_name(stem + EDITED_SUFFIXES[lang] + (extension or ext))
    return duplicate_name(edited, index)


def sidecar_name(media_name: str, *, style: SidecarStyle = "supplemental", index: int = 0) -> str:
    """Sidecar for the on-disk media name ``media_name`` (without its ``(N)`` index).

    The name before ``.json`` is cut so the whole name fits in 51 characters, which trims the
    supplemental suffix first and then the media name itself (``IMG….jp.json``).
    """
    suffix = SUPPLEMENTAL_SUFFIX if style == "supplemental" else ""
    base = (media_name + suffix)[: SIDECAR_NAME_LIMIT - len(".json")]
    return f"{base}({index}).json" if index else f"{base}.json"


# --- Sidecar JSON ------------------------------------------------------------------------

_MONTHS = {
    "en_GB": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"),
    "en_US": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
}

_ORIGINS: dict[Origin, dict[str, object]] = {
    "desktop": {"photosDesktopUploader": {}},
    "mobile": {"mobileUpload": {"deviceType": "IOS_PHONE"}},
    "auto": {"composition": {"type": "AUTO"}},
}


@dataclass(frozen=True)
class Geo:
    latitude: float
    longitude: float
    altitude: float = 0.0


def format_timestamp(when: datetime, locale: Locale = "en_GB") -> str:
    """The ``formatted`` twin of a sidecar timestamp, in the account's locale."""
    moment = utc(when)
    month = _MONTHS[locale][moment.month - 1]
    if locale == "en_GB":
        return f"{moment.day} {month} {moment.year}, {moment:%H:%M:%S} UTC"
    hour = moment.hour % 12 or 12
    meridiem = "AM" if moment.hour < 12 else "PM"
    return f"{month} {moment.day}, {moment.year}, {hour}:{moment:%M:%S} {meridiem} UTC"


def _time(when: datetime, locale: Locale) -> dict[str, str]:
    return {
        "timestamp": str(int(utc(when).timestamp())),
        "formatted": format_timestamp(when, locale),
    }


def _geo(geo: Geo | None) -> dict[str, float]:
    geo = geo or Geo(0.0, 0.0)
    return {
        "latitude": geo.latitude,
        "longitude": geo.longitude,
        "altitude": geo.altitude,
        "latitudeSpan": 0.0,
        "longitudeSpan": 0.0,
    }


def sidecar_payload(
    *,
    title: str,
    taken: datetime,
    created: datetime,
    url: str,
    geo: Geo | None = None,
    geo_exif: Geo | None = None,
    description: str = "",
    favorited: bool = False,
    people: Iterable[str] = (),
    views: int = 0,
    origin: Origin = "desktop",
    locale: Locale = "en_GB",
) -> dict[str, object]:
    """A media sidecar, with the keys in the order Takeout writes them."""
    data: dict[str, object] = {
        "title": title,
        "description": description,
        "imageViews": str(views),
        "creationTime": _time(created, locale),
        "photoTakenTime": _time(taken, locale),
        "geoData": _geo(geo),
    }
    if geo_exif is not None:
        data["geoDataExif"] = _geo(geo_exif)
    names = [{"name": name} for name in people]
    if names:
        data["people"] = names
    data["url"] = url
    data["googlePhotosOrigin"] = _ORIGINS[origin]
    if favorited:
        data["favorited"] = True
    return data


def album_metadata(
    title: str,
    *,
    description: str | None = None,
    comments: Iterable[tuple[str, datetime]] = (),
    locale: Locale = "en_GB",
) -> dict[str, object]:
    """An album folder's ``metadata.json``."""
    data: dict[str, object] = {"title": title}
    if description is not None:
        data["description"] = description
    shared = [
        {"contentOwnerName": owner, "creationTime": _time(when, locale), "liked": True}
        for owner, when in comments
    ]
    if shared:
        data["sharedAlbumComments"] = shared
    return data
