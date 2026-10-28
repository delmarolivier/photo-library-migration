"""Reading a Google Takeout export: its zip parts, or a tree someone already unzipped.

Only ``Takeout/Google Photos/`` matters; everything else is counted and ignored. Nothing here
writes to the zips or to an extracted tree.

A big export comes as several archives, each split into parts:
``takeout-<stamp>-<archive>-<part>.zip``. Google numbers clashing names within one archive but
not across archives, so two archives can hold different files under the same path. An entry's
``key`` tells them apart: the path itself in the first archive, ``Takeout-<archive>/...`` in the
others. ``extract`` copies zip entries into a destination
as read-only originals and refuses to replace one that differs.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

PHOTOS_PREFIX = "Takeout/Google Photos/"
_ARCHIVE = re.compile(r"-(\d+)-\d{3}\.zip$")


class TakeoutError(RuntimeError):
    """The Takeout is missing, unsafe or inconsistent."""


@dataclass(frozen=True)
class Entry:
    path: str
    """``Takeout/Google Photos/<folder>/<name>``."""
    part: str | None
    """The zip file it is in, or ``None`` for an extracted tree."""
    size: int
    crc32: int | None
    modified: datetime
    """The zip entry's (or file's) timestamp, naive local time."""
    archive: str | None = None
    """The archive its part belongs to (``takeout-<stamp>-<archive>-<part>.zip``), if numbered."""

    @property
    def key(self) -> str:
        """The entry's identity in this Takeout, unique across archives."""
        if self.archive in (None, "1"):
            return self.path
        return f"Takeout-{self.archive}/{self.path.removeprefix('Takeout/')}"


def _safe(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise TakeoutError(f"unsafe path in the Takeout: {name!r}")
    return name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _crc32(path: Path) -> int:
    crc = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
    return crc


class Takeout:
    """An opened Takeout. Use ``open_takeout``; close it (or use ``with``) when done."""

    def __init__(self, root: Path, parts: tuple[Path, ...]) -> None:
        self.root = root
        self.parts = parts
        self._zips = {part.name: zipfile.ZipFile(part) for part in parts}
        self._where: dict[str, zipfile.ZipInfo] = {}
        self._part_of: dict[str, str] = {}
        self.ignored = 0
        entries = self._zip_entries() if parts else self._tree_entries()
        self.entries = tuple(sorted(entries, key=lambda e: e.key))
        self._by_locator = {self.locator(entry): entry for entry in self.entries}

    def _zip_entries(self) -> list[Entry]:
        entries = []
        for part, bundle in self._zips.items():
            numbered = _ARCHIVE.search(part)
            archive = numbered.group(1) if numbered else None
            for info in bundle.infolist():
                name = _safe(info.filename)
                if info.is_dir():
                    continue
                if not name.startswith(PHOTOS_PREFIX):
                    self.ignored += 1
                    continue
                entry = Entry(
                    name, part, info.file_size, info.CRC, datetime(*info.date_time), archive
                )
                known = self._where.get(entry.key)
                if known is not None:
                    if (known.CRC, known.file_size) != (info.CRC, info.file_size):
                        raise TakeoutError(f"{name} has different contents in two zip parts")
                    continue
                self._where[entry.key] = info
                self._part_of[entry.key] = part
                entries.append(entry)
        return entries

    def _tree_entries(self) -> list[Entry]:
        entries = []
        for file in self.root.rglob("*"):
            if not file.is_file():
                continue
            name = file.relative_to(self.root).as_posix()
            if not name.startswith(PHOTOS_PREFIX):
                self.ignored += 1
                continue
            info = file.stat()
            entries.append(
                Entry(name, None, info.st_size, None, datetime.fromtimestamp(info.st_mtime))
            )
        return entries

    def read(self, key: str) -> bytes:
        """The bytes of the entry with this ``key``."""
        if not self.parts:
            if not any(entry.key == key for entry in self.entries):
                raise KeyError(key)
            return (self.root / key).read_bytes()
        return self._zips[self._part_of[key]].read(self._where[key])

    def locator(self, entry: Entry) -> str:
        """Where the original is, relative to the Takeout folder: ``<part>::<member>``."""
        return f"{entry.part}::{entry.path}" if entry.part else entry.path

    def entry(self, locator: str) -> Entry:
        return self._by_locator[locator]

    @contextmanager
    def local_copy(self, entry: Entry, scratch: Path) -> Iterator[tuple[Path, str]]:
        """A readable file with ``entry``'s bytes, and their SHA-256, for exiftool and hashing.

        Zip entries are streamed into a temporary file under ``scratch``, removed afterwards.
        Entries of an extracted tree are the originals themselves (only ever read).
        """
        if entry.part is None:
            original = self.root / entry.path
            yield original, _sha256(original)
            return
        with tempfile.TemporaryDirectory(dir=scratch, prefix=".entry-") as holder:
            copy = Path(holder) / f"original{PurePosixPath(entry.path).suffix}"
            digest = hashlib.sha256()
            with (
                self._zips[entry.part].open(self._where[entry.key]) as source,
                copy.open("wb") as target,
            ):
                while chunk := source.read(1 << 20):
                    digest.update(chunk)
                    target.write(chunk)
            yield copy, digest.hexdigest()

    def original(self, entry: Entry, extracted: Path) -> Path:
        """Where the untouched original of ``entry`` lives (once extracted)."""
        return extracted / entry.key if entry.part else self.root / entry.path

    def extract(self, part: str, dest: Path) -> int:
        """Extract the Google Photos entries of one zip part. Returns how many were written."""
        if part not in self._zips:
            raise TakeoutError(f"no part named {part} in this Takeout")
        dest.mkdir(parents=True, exist_ok=True)
        written = 0
        with tempfile.TemporaryDirectory(dir=dest, prefix=".extracting-") as scratch:
            for entry in self.entries:
                if entry.part != part:
                    continue
                target = dest / entry.key
                if target.exists():
                    if target.stat().st_size != entry.size or _crc32(target) != entry.crc32:
                        raise TakeoutError(f"extracted original differs from the zip: {target}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = Path(scratch) / f"{written}"
                staged.write_bytes(self.read(entry.key))
                os.chmod(staged, 0o444)
                stamp = entry.modified.timestamp()
                os.utime(staged, (stamp, stamp))
                os.replace(staged, target)  # no-delete-check: allow (our staged copy)
                written += 1
        return written

    def close(self) -> None:
        for bundle in self._zips.values():
            bundle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def open_takeout(path: Path) -> Takeout:
    """Open the zip parts in ``path`` (``takeout-*.zip``), or an unzipped ``Takeout/`` tree."""
    parts = tuple(sorted(path.glob("takeout-*.zip")))
    if not parts and not (path / PHOTOS_PREFIX).is_dir():
        raise TakeoutError(f"no takeout-*.zip parts and no Takeout/Google Photos/ in {path}")
    takeout = Takeout(path, parts)
    return takeout
