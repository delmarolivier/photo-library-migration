"""``photomigrate index``: a read-only catalogue of the iCloud Photos library (CLA-79).

Nothing in the library is ever written, not even SQLite's ``-shm`` bookkeeping. A WAL reader
updates that file even with ``mode=ro``, so osxphotos reads a *shadow* library instead: a
private clone of ``database/`` (APFS clone where possible, else a copy), with read-only
symlinks to everything else. A ``ReadOnlyGuard`` refuses any write inside the real library for
the whole run (CLAUDE.md rule 6). Items whose originals are not on this Mac
(iCloud "Optimize Mac Storage") become ``index-missing`` actions and are flagged
``original-missing``, so they are reported rather than skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

from photomigrate.catalogue import Catalogue, CatalogueItem, Kind
from photomigrate.embedding import Embedder
from photomigrate.faces import FaceReader
from photomigrate.hashing import bitrate, sha256_file
from photomigrate.ingest.exif import Embedded, ExifTool
from photomigrate.quality import (
    PhotoMeasurement,
    VideoMetrics,
    as_json,
    measure_photo,
    measure_video,
)
from photomigrate.runner import Action, JSONValue
from photomigrate.safety import ReadOnlyGuard


class LibraryError(RuntimeError):
    """The path is not a readable Photos library, or osxphotos is unavailable."""


def _relative(path: str | None, library: Path) -> str | None:
    if not path:
        return None
    real = os.path.realpath(path)
    root = os.path.realpath(library)
    return os.path.relpath(real, root) if real.startswith(root + os.sep) else None


def _details(photo: Any, library: Path) -> dict[str, JSONValue]:
    original = _relative(photo.path, library)
    if photo.ismissing or original is None or not (library / original).exists():
        original = None
    flags = {
        flag
        for flag, present in (
            ("edited", photo.hasadjustments),
            ("live-photo", photo.live_photo),
            ("hidden", photo.hidden),
            ("burst", photo.burst),
            ("shared", photo.shared),
            ("cloud-asset", photo.iscloudasset),
        )
        if present
    }
    return {
        "original": original,
        "filename": photo.original_filename,
        "kind": "video" if photo.ismovie else "photo",
        "date": photo.date.isoformat() if photo.date else None,
        "timezone": photo.tzname,
        "latitude": photo.latitude,
        "longitude": photo.longitude,
        "width": photo.original_width,
        "height": photo.original_height,
        "favorite": bool(photo.favorite),
        "caption": photo.description or None,
        "albums": [[album.uuid, album.title] for album in photo.album_info],
        "keywords": sorted(photo.keywords),
        "scores": cast(JSONValue, photo.score.asdict()),
        "live_motion": _relative(photo.path_live_photo, library),
        "flags": cast(JSONValue, sorted(flags)),
    }


def _clone(source: Path, dest: Path) -> None:
    """Copy a directory tree, as an APFS clone when the volume supports it."""
    if sys.platform == "darwin":
        done = subprocess.run(["cp", "-cR", str(source), str(dest)], capture_output=True)  # noqa: S603, S607
        if done.returncode == 0:
            return
    shutil.copytree(source, dest)


class IndexStage:
    name = "index"

    def __init__(
        self,
        library: Path,
        *,
        embedder: Embedder | None = None,
        faces: FaceReader | None = None,
    ) -> None:
        self.library = library
        self.embedder = embedder
        self.faces = faces
        self.read_only = ReadOnlyGuard(library)
        self._exiftool: ExifTool | None = None
        self._shadow: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Self:
        self.read_only.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._exiftool is not None:
            self._exiftool.close()
        if self._shadow is not None:
            self._shadow.cleanup()
        self.read_only.__exit__()

    def _shadow_library(self) -> Path:
        """A library osxphotos may touch: a clone of database/, symlinks to the rest."""
        self._shadow = tempfile.TemporaryDirectory(prefix="photomigrate-shadow-")
        shadow = Path(self._shadow.name) / self.library.name
        shadow.mkdir()
        for entry in sorted(self.library.iterdir()):
            if entry.name == "database":
                _clone(entry, shadow / "database")
            else:
                (shadow / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
        return shadow

    def _photos(self) -> list[Any]:
        if not (self.library / "database" / "Photos.sqlite").is_file():
            raise LibraryError(f"{self.library} is not a Photos library")
        try:
            import osxphotos
        except ImportError as exc:  # Linux, or osxphotos not installed
            raise LibraryError("indexing needs macOS with osxphotos installed") from exc
        database = osxphotos.PhotosDB(dbfile=str(self._shadow_library()))
        return sorted(database.photos(intrash=False), key=lambda photo: photo.uuid)

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        for photo in self._photos():
            details = _details(photo, self.library)
            kind = "index" if details["original"] else "index-missing"
            yield Action(kind, photo.uuid, details)

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        details = action.details
        kind = cast(Kind, details["kind"])
        flags = {str(flag) for flag in cast(list[str], details["flags"])}
        sha256 = None
        photo = PhotoMeasurement()
        signature: list[str] | None = None
        rate: int | None = None
        video: VideoMetrics | None = None
        embedded = Embedded()
        if details["original"]:
            original = self.library / str(details["original"])
            sha256 = sha256_file(original)
            if self._exiftool is None:
                self._exiftool = ExifTool()
            embedded = self._exiftool.read(original)
            if kind == "photo":
                photo = measure_photo(original, embedder=self.embedder, faces=self.faces)
            else:
                signature, video = measure_video(original, embedded.duration)
                rate = bitrate(original.stat().st_size, embedded.duration)
        else:
            flags.add("original-missing")
        item_id = catalogue.upsert_item(
            CatalogueItem(
                source="icloud",
                source_key=action.target,
                kind=kind,
                original_path=cast(str | None, details["original"]),
                sha256=sha256,
                phash=photo.hashes.phash if photo.hashes else None,
                dhash=photo.hashes.dhash if photo.hashes else None,
                phash_fine=photo.hashes.fine if photo.hashes else None,
                width=cast(int | None, details["width"]) or embedded.width,
                height=cast(int | None, details["height"]) or embedded.height,
                duration=embedded.duration,
                video_signature=",".join(signature) if signature else None,
                bitrate=rate,
                latitude=cast(float | None, details["latitude"]),
                longitude=cast(float | None, details["longitude"]),
                title=cast(str | None, details["filename"]),
                description=cast(str | None, details["caption"]),
                favorite=bool(details["favorite"]),
                flags=frozenset(flags),
                content_id=embedded.content_id,
                camera=embedded.camera,
                scores=json.dumps(details["scores"], sort_keys=True),
                timezone=cast(str | None, details["timezone"]),
                quality=as_json(photo.metrics or video),
                embedding=photo.embedding,
                faces=photo.faces,
            )
        )
        if details["date"]:
            catalogue.set_date_candidate(
                item_id, "icloud", datetime.fromisoformat(str(details["date"]))
            )
        if embedded.taken is not None:
            catalogue.set_date_candidate(
                item_id, "exif" if kind == "photo" else "quicktime", embedded.taken
            )
        for album_uuid, title in cast(list[list[str]], details["albums"]):
            album_id = catalogue.upsert_album("icloud", album_uuid, title)
            catalogue.add_album_member(album_id, item_id)
        catalogue.set_keywords(item_id, "", cast(list[str], details["keywords"]))
