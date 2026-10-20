"""Google Photos JSON sidecars: parsing, and matching each media file to its sidecar.

Matching works one folder at a time, on names alone, and handles the quirks in
docs/TAKEOUT_FORMAT.md:

* supplemental (``x.jpg.supplemental-metadata.json``) and legacy (``x.jpg.json``) sidecars;
* the ``(N)`` index, which moves to just before ``.json``;
* names cut to 47 (media) and 51 (sidecar) characters, accepted as long as the sidecar's
  name is a prefix of the full one;
* legacy sidecars without the media extension (``x.json``);
* edited copies, which use their original's sidecar.

Each media file gets a ``Match``; nothing is dropped. Sidecars nobody claims are orphans.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from photomigrate.ingest.names import MediaName, nfc, parse_media_name

SUPPLEMENTAL = ".supplemental-metadata"
MIN_TRUNCATED_CORE = 40
"""A cut sidecar name must keep at least this many characters to count as a match."""

ALBUM_METADATA_NAMES = frozenset(
    {"metadata.json", "métadonnées.json", "metadaten.json", "metadati.json", "metadatos.json",
     "metadados.json", "metadane.json"}
)  # fmt: skip
OTHER_KNOWN_JSON = frozenset(
    {"print-subscriptions.json", "shared_album_comments.json", "user-generated-memory-titles.json"}
)

MatchMethod = Literal["exact", "truncated", "no-extension", "edited-original", "unmatched"]
Origin = Literal["desktop", "mobile", "auto"]
Position = tuple[float, float, float]

_JSON_NAME = re.compile(r"^(?P<core>.*?)(?:\((?P<index>\d+)\))?\.json$", re.IGNORECASE)
_ORIGIN_KEYS: dict[str, Origin] = {
    "photosDesktopUploader": "desktop",
    "mobileUpload": "mobile",
    "composition": "auto",
}
_ORIGINS: dict[str, Origin] = {origin: origin for origin in _ORIGIN_KEYS.values()}


class SidecarError(ValueError):
    """A sidecar that is not valid Google Photos JSON."""


@dataclass(frozen=True)
class SidecarData:
    title: str | None = None
    description: str = ""
    taken: datetime | None = None
    created: datetime | None = None
    geo: Position | None = None
    geo_exif: Position | None = None
    favorited: bool = False
    people: tuple[str, ...] = ()
    origin: Origin | None = None
    url: str | None = None
    """Google Photos' URL for the item: the same for every copy of it in the export."""

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "description": self.description,
            "taken": self.taken.isoformat() if self.taken else None,
            "created": self.created.isoformat() if self.created else None,
            "geo": list(self.geo) if self.geo else None,
            "geo_exif": list(self.geo_exif) if self.geo_exif else None,
            "favorited": self.favorited,
            "people": list(self.people),
            "origin": self.origin,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SidecarData:
        def when(key: str) -> datetime | None:
            value = data.get(key)
            return datetime.fromisoformat(value) if isinstance(value, str) else None

        def where(key: str) -> Position | None:
            value = data.get(key)
            if not isinstance(value, list):
                return None
            latitude, longitude, altitude = (float(v) for v in value)
            return latitude, longitude, altitude

        people = data.get("people")
        origin = data.get("origin")
        title = data.get("title")
        url = data.get("url")
        return cls(
            title=title if isinstance(title, str) else None,
            description=str(data.get("description") or ""),
            taken=when("taken"),
            created=when("created"),
            geo=where("geo"),
            geo_exif=where("geo_exif"),
            favorited=bool(data.get("favorited")),
            people=tuple(str(p) for p in people) if isinstance(people, list) else (),
            origin=_ORIGINS.get(str(origin)),
            url=url if isinstance(url, str) else None,
        )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, dict) or "timestamp" not in value:
        return None
    try:
        return datetime.fromtimestamp(int(value["timestamp"]), UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SidecarError(f"bad timestamp {value['timestamp']!r}") from exc


def _position(value: object) -> Position | None:
    if not isinstance(value, dict):
        return None
    latitude = float(value.get("latitude", 0.0))
    longitude = float(value.get("longitude", 0.0))
    if latitude == 0.0 and longitude == 0.0:
        return None
    return latitude, longitude, float(value.get("altitude", 0.0))


def parse_sidecar(data: bytes) -> SidecarData:
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SidecarError(f"not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SidecarError("a sidecar must be a JSON object")
    origins = raw.get("googlePhotosOrigin")
    origin = next((_ORIGIN_KEYS[k] for k in origins if k in _ORIGIN_KEYS), None) if isinstance(
        origins, dict
    ) else None  # fmt: skip
    people = raw.get("people")
    title = raw.get("title")
    url = raw.get("url")
    return SidecarData(
        title=title if isinstance(title, str) else None,
        description=str(raw.get("description") or ""),
        taken=_timestamp(raw.get("photoTakenTime")),
        created=_timestamp(raw.get("creationTime")),
        geo=_position(raw.get("geoData")),
        geo_exif=_position(raw.get("geoDataExif")),
        favorited=raw.get("favorited") is True,
        people=tuple(
            str(person["name"])
            for person in people
            if isinstance(person, dict) and "name" in person
        )
        if isinstance(people, list)
        else (),
        origin=origin,
        url=url if isinstance(url, str) else None,
    )


@dataclass(frozen=True)
class AlbumData:
    title: str
    description: str = ""
    shared: bool = False


def parse_album_metadata(data: bytes) -> AlbumData:
    """An album folder's ``metadata.json``: title, description, and whether it is shared."""
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SidecarError(f"album metadata is not JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("title"), str):
        raise SidecarError("album metadata needs a title")
    return AlbumData(
        title=raw["title"],
        description=str(raw.get("description") or ""),
        shared=bool(raw.get("sharedAlbumComments")) or raw.get("access") == "protected",
    )


def is_album_metadata(name: str) -> bool:
    """An album's ``metadata.json`` (the name is localised)."""
    return nfc(name).casefold() in ALBUM_METADATA_NAMES


def is_non_sidecar_json(name: str) -> bool:
    """Album metadata and other Takeout JSON files that describe no media file."""
    return is_album_metadata(name) or nfc(name).casefold() in OTHER_KNOWN_JSON


# --- matching -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    sidecar: str | None
    method: MatchMethod
    original: str | None = None
    """For edited copies: the original's file name, when it is in the folder."""


@dataclass(frozen=True)
class FolderMatch:
    matches: Mapping[str, Match]
    orphans: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class _Sidecar:
    name: str
    core: str
    index: int


def _sidecar(name: str) -> _Sidecar | None:
    parsed = _JSON_NAME.match(nfc(name))
    if parsed is None:
        return None
    return _Sidecar(name, parsed["core"], int(parsed["index"] or 0))


def _score(base: str, stem: str, index: int, sidecar: _Sidecar) -> tuple[int, MatchMethod] | None:
    if sidecar.index != index:
        return None
    core = sidecar.core
    if core in (base + SUPPLEMENTAL, base):
        return 3, "exact"
    if len(core) >= MIN_TRUNCATED_CORE and (base + SUPPLEMENTAL).startswith(core):
        return 2, "truncated"
    if core == stem:
        return 1, "no-extension"
    return None


def _readings(media: MediaName) -> list[tuple[str, str, int]]:
    """(base, stem, index) readings of a name: with its (N) as an index, and taken literally."""
    readings = [(media.base, media.stem, media.index)]
    if media.index:
        literal_stem = media.name[: len(media.name) - len(media.ext)]
        readings.append((media.name, literal_stem, 0))
    return readings


def match_folder(media: Iterable[str], sidecars: Iterable[str]) -> FolderMatch:
    """Match every media file in one folder to the sidecar Takeout wrote for it."""
    parsed = {name: parse_media_name(name) for name in media}
    candidates = [
        s
        for name in sidecars
        if not is_non_sidecar_json(name) and (s := _sidecar(name)) is not None
    ]
    originals = {
        name: p for name, p in parsed.items() if p.edited is None or not _original_of(p, parsed)
    }

    ranked = sorted(
        (
            (score, len(sidecar.core), name, sidecar, method)
            for name, p in originals.items()
            for base, stem, index in _readings(p)
            for sidecar in candidates
            if (scored := _score(base, stem, index, sidecar)) is not None
            for score, method in [scored]
        ),
        key=lambda c: (-c[0], -c[1], c[2], c[3].name),
    )
    matches: dict[str, Match] = {}
    used: set[str] = set()
    for _, _, name, sidecar, method in ranked:
        if name not in matches and sidecar.name not in used:
            matches[name] = Match(sidecar.name, method)
            used.add(sidecar.name)

    for name, p in parsed.items():
        if name in matches:
            continue
        if p.edited is None:
            matches[name] = Match(None, "unmatched")
            continue
        original = _original_of(p, parsed)
        if original is not None:
            found = matches.get(original)
            sidecar_name = found.sidecar if found else None
        else:
            sidecar_name = _sidecar_by_original_stem(p, candidates)
        matches[name] = Match(
            sidecar_name, "edited-original" if sidecar_name else "unmatched", original
        )
        if sidecar_name:
            used.add(sidecar_name)

    orphans = tuple(sorted(s.name for s in candidates if s.name not in used))
    return FolderMatch(matches, orphans)


def _original_of(edit: MediaName, folder: Mapping[str, MediaName]) -> str | None:
    """The original media in the folder for an edited copy: same stem and index."""
    found = [
        name
        for name, p in folder.items()
        if p.edited is None and p.stem == edit.edited and p.index == edit.index
    ]
    # Same extension first, then the same ignoring case (edits are sometimes .jpg next to .JPG).
    found.sort(
        key=lambda name: (
            folder[name].ext != edit.ext,
            folder[name].ext.casefold() != edit.ext.casefold(),
            name,
        )
    )
    return found[0] if found else None


def _sidecar_by_original_stem(edit: MediaName, sidecars: Iterable[_Sidecar]) -> str | None:
    """An edited copy whose original is missing: a sidecar for ``<stem>.<any ext>``."""
    hits = []
    for sidecar in sidecars:
        base = sidecar.core.removesuffix(SUPPLEMENTAL)
        reading = parse_media_name(base)
        if reading.stem == edit.edited and sidecar.index == edit.index and reading.ext:
            hits.append(sidecar.name)
    return hits[0] if len(hits) == 1 else None
