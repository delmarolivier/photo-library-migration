"""Synthetic Google Takeout exports for tests (M1-02).

The naming rules follow docs/TAKEOUT_FORMAT.md. Rules seen in Delmar's real sample:
``.supplemental-metadata.json`` sidecars, ``(N)`` moving to just before ``.json``, and
``-edited`` copies without their own sidecar. Rules modelled on public reports (the
google-photos-takeout-helper project) and not in the sample: legacy ``.json`` sidecars,
47-character media names, 51-character sidecar names, and localised edit suffixes. If a real
export disagrees, fix the model here and in the doc.
"""

from __future__ import annotations

import dataclasses
import json
import random
import string
import uuid
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal

from PIL import Image, ImageChops, ImageEnhance

from photomigrate.testing.media import (
    Container,
    Exif,
    ImageFormat,
    encode_gif,
    encode_image,
    encode_video,
    synthetic_image,
    utc,
    video_frames,
)

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


# --- Builder -----------------------------------------------------------------------------

FIXTURE_NOW = datetime(2026, 10, 20, 8, 42, 20, tzinfo=UTC)
"""When the synthetic export was "made"; later dates count as future dates."""

PHOTOS_ROOT = "Takeout/Google Photos"
EXPORT_STAMP = "20261020T084220Z"
PLACEHOLDER_DATES = frozenset({date(1970, 1, 1), date(1980, 1, 1), date(2000, 1, 1)})

Kind = Literal["photo", "video"]
NearDuplicateKind = Literal["resized", "recompressed", "heic-vs-jpeg"]
CreationKind = Literal["MOTION", "MIX", "SMILE"]

_ARCHIVE_BROWSER = "Takeout/archive_browser.html"
_ZIP_TIME = (2026, 10, 20, 8, 42, 20)
_IMAGE_FORMATS: dict[str, ImageFormat] = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".heic": "HEIC",
    ".png": "PNG",
}
_CONTAINERS: dict[str, Container] = {".mp4": "mp4", ".mov": "mov"}


class _Default(Enum):
    SAME = "same"


SAME = _Default.SAME
"""``exif_taken`` default: the embedded capture time equals ``taken``."""


@dataclass(frozen=True)
class Item:
    """Ground truth for one media file in a generated Takeout. Relations hold paths."""

    path: str
    kind: Kind
    folder: str
    sidecar: str | None
    taken: datetime
    created: datetime
    embedded_taken: datetime | None
    geo: Geo | None = None
    description: str = ""
    favorited: bool = False
    people: tuple[str, ...] = ()
    copy_of: str | None = None
    edit_of: str | None = None
    live_partner: str | None = None
    near_duplicate_of: str | None = None
    near_duplicate_kind: NearDuplicateKind | None = None
    google_creation: CreationKind | None = None
    content_id: str | None = None
    """The Live Photo identifier the pair shares (embedded in the motion clip only: Apple keeps
    the still's copy in MakerNotes, which fixtures cannot write)."""
    quirks: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    def to_dict(self) -> dict[str, object]:
        data = dataclasses.asdict(self)
        for key in ("taken", "created", "embedded_taken"):
            value = getattr(self, key)
            data[key] = value.isoformat() if value else None
        data["people"] = list(self.people)
        data["quirks"] = sorted(self.quirks)
        return data


@dataclass(frozen=True)
class TakeoutManifest:
    """Everything a test needs to know about a generated Takeout."""

    items: tuple[Item, ...]
    albums: tuple[str, ...]
    files: tuple[str, ...]
    root: Path | None = None
    zips: tuple[Path, ...] = ()
    parts: Mapping[str, int] = field(default_factory=dict)

    def item(self, path: str) -> Item:
        for item in self.items:
            if item.path == path:
                return item
        raise KeyError(path)

    def with_quirk(self, quirk: str) -> list[Item]:
        return [item for item in self.items if quirk in item.quirks]

    def metadata_sidecar(self, item: Item) -> str | None:
        """The sidecar whose metadata applies: the item's own, or the original's for edits."""
        if item.sidecar is None and item.edit_of is not None:
            return self.item(item.edit_of).sidecar
        return item.sidecar

    def to_json(self) -> str:
        return json.dumps(
            {
                "albums": list(self.albums),
                "files": list(self.files),
                "zips": [archive.name for archive in self.zips],
                "items": [item.to_dict() for item in self.items],
            },
            ensure_ascii=False,
            indent=2,
        )


@dataclass(frozen=True)
class _Meta:
    geo: Geo | None = None
    geo_in_exif: bool = False
    description: str = ""
    favorited: bool = False
    people: tuple[str, ...] = ()
    locale: Locale = "en_GB"
    origin: Origin = "desktop"


@dataclass(frozen=True)
class _Source:
    """What derived files (copies, edits, near-duplicates, creations) reuse."""

    image: Image.Image
    exif: Exif | None
    base: str
    index: int


def _is_placeholder(when: datetime) -> bool:
    day = when.date() if when.tzinfo is None else utc(when).date()
    return day in PLACEHOLDER_DATES


def _suffix(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def _image_format(name: str) -> ImageFormat:
    try:
        return _IMAGE_FORMATS[_suffix(name)]
    except KeyError:
        raise ValueError(f"not a photo extension: {name}") from None


def _container(name: str) -> Container:
    try:
        return _CONTAINERS[_suffix(name)]
    except KeyError:
        raise ValueError(f"not a video extension: {name}") from None


def _json(data: Mapping[str, object]) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode()


def _camera_exif(
    camera: tuple[str, str] | None, taken: datetime | None, gps: Geo | None
) -> Exif | None:
    if camera is None and taken is None:
        return None
    make, model = camera or (None, None)
    return Exif(
        taken=taken, make=make, model=model, gps=(gps.latitude, gps.longitude) if gps else None
    )


class TakeoutBuilder:
    """Assembles a synthetic Google Photos Takeout in memory, then writes a tree or zips.

    Every media file becomes an ``Item`` recording the truth about it (dates, sidecar,
    relations to other files, quirk tags) for tests to assert against.
    """

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)
        self._files: dict[str, bytes] = {_ARCHIVE_BROWSER: _ARCHIVE_BROWSER_HTML}
        self._items: dict[str, Item] = {}
        self._sources: dict[str, _Source] = {}
        self._albums: list[str] = []

    # -- media ----------------------------------------------------------------------------

    def photo(
        self,
        folder: str,
        name: str,
        *,
        taken: datetime,
        created: datetime | None = None,
        camera: tuple[str, str] | None = ("Apple", "iPhone 8"),
        exif_taken: datetime | Literal[_Default.SAME] | None = SAME,
        geo: Geo | None = None,
        geo_in_exif: bool = False,
        description: str = "",
        favorited: bool = False,
        people: Sequence[str] = (),
        sidecar: SidecarStyle | None = "supplemental",
        index: int = 0,
        locale: Locale = "en_GB",
        origin: Origin = "desktop",
        size: tuple[int, int] = (320, 240),
        image: Image.Image | None = None,
        quirks: Iterable[str] = (),
    ) -> Item:
        embedded = taken if exif_taken is SAME else exif_taken
        meta = _Meta(geo, geo_in_exif, description, favorited, tuple(people), locale, origin)
        exif = _camera_exif(camera, embedded, geo if geo_in_exif else None)
        picture = image or synthetic_image(self._seed(), size)
        return self._photo(
            folder, name, picture=picture, exif=exif, embedded=embedded, taken=taken,
            created=created, sidecar=sidecar, index=index, meta=meta, quirks=quirks,
        )  # fmt: skip

    def video(
        self,
        folder: str,
        name: str,
        *,
        taken: datetime,
        created: datetime | None = None,
        seconds: float = 1.0,
        geo: Geo | None = None,
        description: str = "",
        people: Sequence[str] = (),
        sidecar: SidecarStyle | None = "supplemental",
        index: int = 0,
        locale: Locale = "en_GB",
        frames: Sequence[Image.Image] | None = None,
        content_id: str | None = None,
        quirks: Iterable[str] = (),
    ) -> Item:
        clip = list(frames or video_frames(self._seed(), seconds))
        data = encode_video(clip, _container(name), created=taken, content_id=content_id)
        meta = _Meta(geo=geo, description=description, people=tuple(people), locale=locale)
        return self._store(
            folder, name, kind="video", data=data, source=_Source(clip[0], None, "", 0),
            taken=taken, created=created, embedded=taken, sidecar=sidecar, index=index,
            meta=meta, quirks=quirks,
        )  # fmt: skip

    def copy_to(self, item: Item, folder: str) -> Item:
        """The same item again in another album folder: identical bytes and sidecar."""
        path = f"{PHOTOS_ROOT}/{folder}/{item.name}"
        self._add(path, self._files[item.path])
        sidecar = None
        if item.sidecar:
            sidecar = f"{PHOTOS_ROOT}/{folder}/{PurePosixPath(item.sidecar).name}"
            self._add(sidecar, self._files[item.sidecar])
        copy = replace(
            item,
            path=path,
            folder=folder,
            sidecar=sidecar,
            copy_of=item.copy_of or item.path,
            quirks=item.quirks | {"album-copy"},
        )
        return self._keep(copy, self._sources[item.path])

    def edited(self, item: Item, *, lang: str = "en", extension: str | None = None) -> Item:
        """Google's edited copy: next to the original, same metadata, no sidecar of its own."""
        source = self._sources[item.path]
        name = edited_name(source.base, lang=lang, extension=extension, index=source.index)
        path = f"{PHOTOS_ROOT}/{item.folder}/{name}"
        picture = ImageEnhance.Brightness(source.image).enhance(1.25)
        self._add(path, encode_image(picture, _image_format(name), exif=source.exif))
        quirks = {"edited", f"edited-{lang}", "uses-original-sidecar"}
        if extension is not None and extension != PurePosixPath(source.base).suffix:
            quirks.add("edited-extension-case")
        if EDITED_SUFFIXES[lang] not in name:
            quirks.add("truncated-edit-suffix")
        edit = replace(
            item, path=path, sidecar=None, edit_of=item.path, copy_of=None, quirks=frozenset(quirks)
        )
        return self._keep(edit, replace(source, image=picture))

    def live_photo(
        self,
        folder: str,
        stem: str,
        *,
        taken: datetime,
        created: datetime | None = None,
        still_ext: str = ".HEIC",
        motion_ext: str = ".MOV",
        motion_sidecar: bool = False,
        sidecar: SidecarStyle = "supplemental",
        geo: Geo | None = None,
        image: Image.Image | None = None,
    ) -> tuple[Item, Item]:
        """A Live Photo as Takeout splits it: a still plus a short motion clip."""
        picture = image or synthetic_image(self._seed())
        still = self.photo(
            folder, stem + still_ext, taken=taken, created=created, geo=geo, sidecar=sidecar,
            image=picture, quirks={"live-photo"},
        )  # fmt: skip
        small = picture.resize((64, 48))
        content_id = str(uuid.UUID(int=self._rng.getrandbits(128))).upper()
        motion = self.video(
            folder, stem + motion_ext, taken=taken, created=still.created, geo=geo,
            sidecar=sidecar if motion_sidecar else None,
            frames=[ImageChops.offset(small, step * 2, 0) for step in range(15)],
            content_id=content_id, quirks={"live-photo", "live-photo-motion"},
        )  # fmt: skip
        still = self._keep(
            replace(still, live_partner=motion.path, content_id=content_id),
            self._sources[still.path],
        )
        motion = self._keep(
            replace(motion, live_partner=still.path, content_id=content_id),
            self._sources[motion.path],
        )
        return still, motion

    def near_duplicate(
        self,
        item: Item,
        folder: str,
        name: str,
        *,
        kind: NearDuplicateKind,
        strip_exif: bool = False,
        taken: datetime | None = None,
        created: datetime | None = None,
        sidecar: SidecarStyle | None = "supplemental",
    ) -> Item:
        """The same picture again, resized, recompressed, or in the other format."""
        source = self._sources[item.path]
        picture, quality = source.image, 85
        if kind == "resized":
            picture = picture.resize((picture.width // 2, picture.height // 2))
        elif kind == "recompressed":
            quality = 25
        elif _image_format(name) == _image_format(item.name):
            raise ValueError("heic-vs-jpeg needs a different format from the original")
        exif = None if strip_exif else source.exif
        duplicate = self._photo(
            folder, name, picture=picture, exif=exif,
            embedded=exif.taken if exif else None, taken=taken or item.taken, created=created,
            sidecar=sidecar, index=0, meta=_Meta(), quality=quality,
            quirks={"near-duplicate", f"near-duplicate-{kind}"},
        )  # fmt: skip
        return self._keep(
            replace(duplicate, near_duplicate_of=item.path, near_duplicate_kind=kind),
            self._sources[duplicate.path],
        )

    def google_creation(self, item: Item, kind: CreationKind) -> Item:
        """An animation or composite Google made from ``item`` (not a camera original)."""
        source = self._sources[item.path]
        stem = PurePosixPath(source.base).stem
        if kind == "MOTION":
            name = f"{stem}-MOTION.gif"
            frames = [
                ImageChops.offset(source.image.resize((96, 72)), step * 3, 0) for step in range(6)
            ]
            picture, data = frames[0], encode_gif(frames)
        else:
            name = f"{stem}-{kind}.jpg"
            picture = ImageEnhance.Color(source.image).enhance(1.6)
            data = encode_image(picture, "JPEG")
        creation = self._store(
            item.folder, name, kind="photo", data=data, source=_Source(picture, None, "", 0),
            taken=item.taken, created=item.created + timedelta(hours=6), embedded=None,
            sidecar="supplemental", index=0, meta=_Meta(origin="auto"),
            quirks={"google-creation", f"google-creation-{kind.lower()}", "no-exif"},
        )  # fmt: skip
        return self._keep(replace(creation, google_creation=kind), self._sources[creation.path])

    # -- other files ----------------------------------------------------------------------

    def album(
        self,
        folder: str,
        *,
        description: str | None = None,
        comments: Iterable[tuple[str, datetime]] = (),
    ) -> None:
        """Give an album folder its ``metadata.json``."""
        payload = album_metadata(folder, description=description, comments=comments)
        self._add(f"{PHOTOS_ROOT}/{folder}/metadata.json", _json(payload))
        self._albums.append(folder)

    def other_product(self, path: str, data: bytes) -> None:
        """A file from another Google product, which ingest must ignore."""
        self._add(f"Takeout/{path}", data)

    # -- output ---------------------------------------------------------------------------

    def files(self) -> dict[str, bytes]:
        return dict(self._files)

    def manifest(
        self,
        *,
        root: Path | None = None,
        zips: tuple[Path, ...] = (),
        parts: Mapping[str, int] | None = None,
    ) -> TakeoutManifest:
        return TakeoutManifest(
            items=tuple(self._items.values()),
            albums=tuple(self._albums),
            files=tuple(self._files),
            root=root,
            zips=zips,
            parts=dict(parts or {}),
        )

    def write_tree(self, dest: Path) -> TakeoutManifest:
        """Write the export as if already unzipped under ``dest``."""
        for path, data in self._files.items():
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return self.manifest(root=dest)

    def write_zips(
        self, dest: Path, *, parts: int = 1, export: str = EXPORT_STAMP
    ) -> TakeoutManifest:
        """Write ``takeout-<export>-NNN.zip`` parts. Files are dealt round-robin in path
        order, so media and sidecars often land in different parts, as in real exports."""
        dest.mkdir(parents=True, exist_ok=True)
        assignment = {_ARCHIVE_BROWSER: 1}
        others = sorted(path for path in self._files if path != _ARCHIVE_BROWSER)
        assignment.update({path: i % parts + 1 for i, path in enumerate(others)})
        archives = []
        for number in range(1, parts + 1):
            archive = dest / f"takeout-{export}-{number:03d}.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                for path in sorted(p for p, n in assignment.items() if n == number):
                    info = zipfile.ZipInfo(path, date_time=_ZIP_TIME)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    bundle.writestr(info, self._files[path])
            archives.append(archive)
        return self.manifest(root=dest, zips=tuple(archives), parts=assignment)

    # -- internals ------------------------------------------------------------------------

    def _seed(self) -> int:
        return self._rng.randrange(2**31)

    def _url(self) -> str:
        alphabet = string.ascii_letters + string.digits
        return "https://photos.google.com/photo/AF1Qip" + "".join(
            self._rng.choice(alphabet) for _ in range(30)
        )

    def _add(self, path: str, data: bytes) -> None:
        if path in self._files:
            raise ValueError(f"{path} is already in this Takeout")
        self._files[path] = data

    def _keep(self, item: Item, source: _Source) -> Item:
        self._items[item.path] = item
        self._sources[item.path] = source
        return item

    def _default_created(self, taken: datetime) -> datetime:
        if _is_placeholder(taken) or taken > FIXTURE_NOW:
            return FIXTURE_NOW - timedelta(days=30)
        return min(taken + timedelta(days=2, hours=3), FIXTURE_NOW - timedelta(hours=1))

    def _photo(
        self,
        folder: str,
        name: str,
        *,
        picture: Image.Image,
        exif: Exif | None,
        embedded: datetime | None,
        taken: datetime,
        created: datetime | None,
        sidecar: SidecarStyle | None,
        index: int,
        meta: _Meta,
        quirks: Iterable[str],
        quality: int = 85,
    ) -> Item:
        data = encode_image(picture, _image_format(name), exif=exif, quality=quality)
        tags = set(quirks) | ({"no-exif"} if exif is None else set())
        return self._store(
            folder, name, kind="photo", data=data, source=_Source(picture, exif, "", 0),
            taken=taken, created=created, embedded=embedded, sidecar=sidecar, index=index,
            meta=meta, quirks=tags,
        )  # fmt: skip

    def _store(
        self,
        folder: str,
        name: str,
        *,
        kind: Kind,
        data: bytes,
        source: _Source,
        taken: datetime,
        created: datetime | None,
        embedded: datetime | None,
        sidecar: SidecarStyle | None,
        index: int,
        meta: _Meta,
        quirks: Iterable[str],
    ) -> Item:
        base = truncate_media_name(name)
        path = f"{PHOTOS_ROOT}/{folder}/{duplicate_name(base, index)}"
        created = created or self._default_created(taken)
        self._add(path, data)
        sidecar_path = None
        if sidecar is not None:
            sidecar_path = (
                f"{PHOTOS_ROOT}/{folder}/{sidecar_name(base, style=sidecar, index=index)}"
            )
            payload = sidecar_payload(
                title=name, taken=taken, created=created, url=self._url(), geo=meta.geo,
                geo_exif=meta.geo if meta.geo_in_exif else None, description=meta.description,
                favorited=meta.favorited, people=meta.people, origin=meta.origin,
                locale=meta.locale,
            )  # fmt: skip
            self._add(sidecar_path, _json(payload))
        tags = set(quirks) | _quirks(name, base, index, sidecar, taken, created, embedded, meta)
        item = Item(
            path=path, kind=kind, folder=folder, sidecar=sidecar_path, taken=taken,
            created=created, embedded_taken=embedded, geo=meta.geo, description=meta.description,
            favorited=meta.favorited, people=meta.people, quirks=frozenset(tags),
        )  # fmt: skip
        return self._keep(item, replace(source, base=base, index=index))


def _quirks(
    name: str,
    base: str,
    index: int,
    sidecar: SidecarStyle | None,
    taken: datetime,
    created: datetime,
    embedded: datetime | None,
    meta: _Meta,
) -> set[str]:
    tags = set()
    if base != name:
        tags.add("truncated-name")
    if index:
        tags.add("duplicate-index")
    if sidecar is None:
        tags.add("missing-sidecar")
    else:
        tags.add(f"{sidecar}-sidecar")
        suffix = SUPPLEMENTAL_SUFFIX if sidecar == "supplemental" else ""
        if len(base + suffix + ".json") > SIDECAR_NAME_LIMIT:
            tags.add("truncated-sidecar")
    if embedded is None:
        tags.add("no-exif-date")
    if _is_placeholder(taken) or (embedded is not None and _is_placeholder(embedded)):
        tags.add("placeholder-date")
    if taken > FIXTURE_NOW:
        tags.add("future-date")
    if taken == created:
        tags.add("upload-date-as-taken")
    if meta.geo is None:
        tags.add("no-geo")
    elif meta.geo_in_exif:
        tags.add("geo-in-exif")
    if meta.description and not meta.description.isascii():
        tags.add("unicode-description")
    if meta.favorited:
        tags.add("favourite")
    if meta.people:
        tags.add("people")
    return tags


# --- The standard scenario ---------------------------------------------------------------

_PARIS = timezone(timedelta(hours=2))
_NICE = Geo(43.6970333, 7.266, 12.0)
_TOKYO = Geo(35.6585805, 139.7454329, 40.0)
_ALICE, _BOB = "Alice Example", "Bob Example"


def _at(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_PARIS)


def standard_takeout(seed: int = 0) -> TakeoutBuilder:
    """One export containing every known Takeout quirk once. See tests/fixtures/README.md."""
    b = TakeoutBuilder(seed)
    y19, y15 = "Photos from 2019", "Photos from 2015"

    # Ordinary items, one per format, with location, people, favourite and unicode text.
    beach = b.photo(
        y19, "IMG_1001.JPG", taken=_at(2019, 7, 1, 12, 34), geo=_NICE, geo_in_exif=True,
        description="Plage à Nice 🏖️", favorited=True, people=(_ALICE, _BOB),
    )  # fmt: skip
    b.photo(y19, "IMG_1002.JPG", taken=_at(2019, 7, 2, 9, 15), locale="en_US")
    tower = b.photo(
        y19, "IMG_1003.HEIC", taken=_at(2019, 7, 20, 18, 5), geo=_TOKYO,
        description="東京タワー", people=(_ALICE,),
    )  # fmt: skip

    # The same item in three albums.
    b.album("Été 2019", description="Vacances 🌞", comments=[(_BOB, _at(2019, 8, 1))])
    b.album("Best of")
    b.album("Family")
    for album in ("Été 2019", "Best of", "Family"):
        b.copy_to(beach, album)

    # Duplicate names, edited copies and their variants.
    b.photo(y19, "IMG_1004.JPG", taken=_at(2019, 7, 3, 10, 0))
    b.edited(b.photo(y19, "IMG_1004.JPG", index=1, taken=_at(2019, 7, 3, 10, 1)))
    b.edited(beach)
    b.edited(b.photo(y19, "IMG_1005.JPG", taken=_at(2019, 7, 4, 11, 0)), extension=".jpg")
    b.edited(
        b.photo(y19, "Screenshot_20191105-123456_LongName_ABCD.jpg", taken=_at(2019, 11, 5),
                camera=None)
    )  # fmt: skip

    # Long names, truncated differently for media and sidecars.
    b.photo(
        y19, "Screenshot_20191105-123456_A_Rather_Long_Application_Name.jpg",
        taken=_at(2019, 11, 5, 12, 35), camera=None,
    )  # fmt: skip

    # An older export era: legacy sidecars, mobile uploads, US dates, localised edits.
    legacy = b.photo(
        y15, "IMG_0500.jpg", taken=_at(2015, 8, 12, 18, 30), sidecar="legacy", origin="mobile",
        locale="en_US",
    )  # fmt: skip
    b.edited(legacy, lang="fr")
    b.edited(
        b.photo(y15, "IMG_0500.jpg", taken=_at(2015, 8, 12, 18, 31), sidecar="legacy", index=1),
        lang="de",
    )
    b.photo(
        y15, "DSC_20150812_183000_family_reunion_at_the_lake_house.jpg",
        taken=_at(2015, 8, 12, 18, 40), sidecar="legacy", camera=("SONY", "DSC-RX100"),
    )  # fmt: skip

    # Live Photos split into still + motion.
    b.live_photo(y19, "IMG_2001", taken=_at(2019, 7, 5, 16, 20))
    b.live_photo(
        y15, "IMG_2002", taken=_at(2015, 8, 13, 9, 0), still_ext=".JPG", motion_ext=".MP4",
        motion_sidecar=True, sidecar="legacy",
    )  # fmt: skip

    # Videos, one without a sidecar, and a photo without one.
    b.video(y19, "IMG_1010.MOV", taken=_at(2019, 7, 6, 14, 0))
    b.video(y19, "VID_20190704_101010.mp4", taken=_at(2019, 7, 4, 10, 10), seconds=2.0)
    b.video(y19, "IMG_1011.MOV", taken=_at(2019, 7, 7, 15, 0), sidecar=None)
    b.photo(y19, "IMG_1006.JPG", taken=_at(2019, 7, 8, 8, 0), sidecar=None)

    # Suspicious dates.
    b.photo(
        "Photos from 1970", "IMG_0001.JPG", taken=datetime(1970, 1, 1, tzinfo=UTC), camera=None,
        exif_taken=None,
    )  # fmt: skip
    b.photo(
        "Photos from 1980", "IMG_1007.JPG", taken=datetime(1980, 1, 1, tzinfo=UTC),
        exif_taken=datetime(1980, 1, 1),
    )  # fmt: skip
    b.photo(
        "Photos from 2000", "DSCN0001.JPG", taken=datetime(2000, 1, 1, tzinfo=UTC),
        exif_taken=datetime(2000, 1, 1), camera=("NIKON", "COOLPIX L1"),
    )  # fmt: skip
    b.photo("Photos from 2037", "IMG_1008.JPG", taken=datetime(2037, 6, 1, 9, 0, tzinfo=UTC))
    uploaded = _at(2019, 8, 20, 21, 0)
    b.photo(
        y19, "IMG_20120304_101112.jpg", taken=uploaded, created=uploaded, camera=None,
        exif_taken=None,
    )  # fmt: skip

    # Near-duplicates: a messaging forward, a recompressed copy, a JPEG export of a HEIC.
    b.near_duplicate(
        beach, y19, "IMG-20190702-WA0003.jpg", kind="resized", strip_exif=True,
        taken=_at(2019, 7, 2, 8, 0),
    )  # fmt: skip
    b.near_duplicate(beach, y19, "received_1001.jpeg", kind="recompressed")
    b.album("Exports")
    b.near_duplicate(tower, "Exports", "IMG_1003.jpg", kind="heic-vs-jpeg")

    # Google-made creations, including an edited one.
    b.google_creation(beach, "MOTION")
    b.google_creation(beach, "MIX")
    b.edited(b.google_creation(tower, "SMILE"))

    # Another Google product in the same export.
    b.other_product(
        "Maps (your places)/Saved Places.json", b'{"type": "FeatureCollection", "features": []}'
    )
    return b


_ARCHIVE_BROWSER_HTML = (
    b"<!DOCTYPE html><html><head><title>Archive browser</title></head>"
    b"<body><h1>Google Takeout</h1></body></html>\n"
)
