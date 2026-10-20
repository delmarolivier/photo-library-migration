"""Reading Takeout media file names: kind, ``(N)`` index, edited copies, embedded dates.

This is the parser side. ``photomigrate.testing.takeout`` independently models how Google
*writes* names, so tests can check one against the other.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

Kind = Literal["photo", "video"]

PHOTO_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp",
     ".dng", ".raw", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2"}
)  # fmt: skip
VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg"}
)

MEDIA_NAME_LIMIT = 47
"""Takeout cuts media names to this many characters (see docs/TAKEOUT_FORMAT.md)."""

EDITED_SUFFIXES = (
    "-edited",
    "-modifié",
    "-bearbeitet",
    "-modificato",
    "-bewerkt",
    "-edytowane",
    "-編集済み",
)

_INDEXED = re.compile(r"^(?P<stem>.*)\((?P<index>\d+)\)$")


@dataclass(frozen=True)
class MediaName:
    """A parsed media file name. ``base`` is the name without its ``(N)`` index."""

    name: str
    base: str
    index: int
    stem: str
    ext: str
    edited: str | None
    """The original's stem, for edited copies."""


def nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def media_kind(name: str) -> Kind | None:
    ext = PurePosixPath(name).suffix.lower()
    if ext in PHOTO_EXTENSIONS:
        return "photo"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return None


def _edited_original(stem: str, name_length: int) -> str | None:
    for suffix in EDITED_SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    if name_length == MEDIA_NAME_LIMIT:  # the limit may have cut the suffix short
        for suffix in EDITED_SUFFIXES:
            for cut in range(len(suffix) - 1, 2, -1):
                if stem.endswith(suffix[:cut]):
                    return stem[:-cut]
    return None


def parse_media_name(name: str) -> MediaName:
    name = nfc(name)
    path = PurePosixPath(name)
    ext = path.suffix
    stem = name[: len(name) - len(ext)]
    index = 0
    indexed = _INDEXED.match(stem)
    if indexed:
        stem, index = indexed["stem"], int(indexed["index"])
    return MediaName(
        name=name,
        base=stem + ext,
        index=index,
        stem=stem,
        ext=ext,
        edited=_edited_original(stem, len(stem) + len(ext)),  # the cut comes before any (N)
    )


_DATE_PATTERNS = (
    # IMG_20120304_101112, VID_…, PXL_…123, PANO_…, Screenshot_20191105-123456, 20190101_101010
    re.compile(r"(?<!\d)(?P<y>\d{4})(?P<m>\d\d)(?P<d>\d\d)[_-](?P<H>\d\d)(?P<M>\d\d)(?P<S>\d\d)"),
    # macOS screenshots: "Screenshot 2019-11-05 at 12.34.56", "Screen Shot … at 1.34.56 PM"
    re.compile(
        r"(?<!\d)(?P<y>\d{4})-(?P<m>\d\d)-(?P<d>\d\d) at (?P<H>\d{1,2})\.(?P<M>\d\d)\.(?P<S>\d\d)"
        r"(?: (?P<half>[AP]M))?"
    ),
    # 2019-07-01 12.34.56 (Dropbox, Apple exports), signal-2021-02-03-040506,
    # photo_2021-02-03_04-05-06 (Telegram), Screenshot_2019-11-05-12-34-56-789 (Android)
    re.compile(
        r"(?<!\d)(?P<y>\d{4})-(?P<m>\d\d)-(?P<d>\d\d)[ _-]"
        r"(?P<H>\d\d)[.-]?(?P<M>\d\d)[.-]?(?P<S>\d\d)"
    ),
    # Dates only, read as midnight: IMG-20190702-WA0003 (WhatsApp), IMG_20120304, 2012-03-04
    re.compile(r"(?<!\d)(?P<y>\d{4})(?P<m>\d\d)(?P<d>\d\d)-WA\d+"),
    re.compile(r"(?<!\d)(?P<y>\d{4})(?P<m>\d\d)(?P<d>\d\d)(?!\d)"),
    re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<m>\d\d)-(?P<d>\d\d)(?!\d)"),
)


def date_from_filename(name: str) -> datetime | None:
    """A capture time encoded in the file name (local time, naive), if there is one.

    The first pattern that matches decides; an impossible date there means no date at all.
    """
    for pattern in _DATE_PATTERNS:
        found = pattern.search(name)
        if not found:
            continue
        fields = found.groupdict()
        hour = int(fields.get("H") or 0)
        if fields.get("half"):
            hour = hour % 12 + (12 if fields["half"] == "PM" else 0)
        try:
            when = datetime(
                int(fields["y"]), int(fields["m"]), int(fields["d"]),
                hour, int(fields.get("M") or 0), int(fields.get("S") or 0),
            )  # fmt: skip
        except ValueError:
            return None
        return when if 1990 <= when.year <= 2100 else None
    return None
