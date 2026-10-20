"""Thumbnails for the review gallery, made once and cached on disk.

Google items are read from the Takeout zips (never extracted, never written), iCloud items
from the library's originals, read-only. Photos are decoded like everywhere else
(``hashing.open_picture``: upright, HEIC too); videos get a poster frame from ffmpeg. Each
thumbnail is a JPEG, at most 320 px on its long side, cached under ``thumbs/`` by the item's
SHA-256. An item whose file is not at hand gets a grey placeholder, which is not cached.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

from photomigrate.catalogue import CatalogueItem
from photomigrate.hashing import open_picture
from photomigrate.ingest.takeout import Entry, Takeout, open_takeout

SIZE = (320, 320)


def _jpeg(image: Image.Image) -> bytes:
    small = image.convert("RGB")
    small.thumbnail(SIZE)
    buffer = io.BytesIO()
    small.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


def placeholder() -> bytes:
    return _jpeg(Image.new("RGB", (160, 120), (200, 200, 200)))


def poster(path: Path) -> Image.Image | None:
    """A video's frame half a second in (or its first), or ``None``."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    for seek in ("0.5", "0"):
        args = [
            ffmpeg, "-nostdin", "-loglevel", "error", "-ss", seek, "-i", str(path),
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
        ]  # fmt: skip
        # Fixed argv: our own values plus the file path; no shell.
        done = subprocess.run(args, capture_output=True, check=False)  # noqa: S603
        if done.returncode == 0 and done.stdout:
            return Image.open(io.BytesIO(done.stdout))
    return None


class Thumbnails:
    def __init__(
        self, cache: Path, *, takeout: Path | None = None, library: Path | None = None
    ) -> None:
        self.cache = cache
        self.takeout_dir = takeout
        self.library = library
        self._takeout: Takeout | None = None
        self._entries: dict[str, Entry] = {}

    def close(self) -> None:
        if self._takeout is not None:
            self._takeout.close()

    def _cached(self, item: CatalogueItem) -> Path:
        stamp = (
            item.sha256 or hashlib.sha256(f"{item.source}:{item.source_key}".encode()).hexdigest()
        )
        return self.cache / stamp[:2] / f"{stamp}.jpg"

    def get(self, item: CatalogueItem) -> bytes:
        cached = self._cached(item)
        if cached.exists():
            return cached.read_bytes()
        with (
            tempfile.TemporaryDirectory(prefix="photomigrate-thumb-") as scratch,
            self._local(item, Path(scratch)) as source,
        ):
            image = None
            if source is not None:
                if item.kind == "photo":
                    picture = open_picture(source)
                    image = None if picture is None else picture.image
                else:
                    image = poster(source)
            if image is None:
                return placeholder()
            data = _jpeg(image)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
        return data

    @contextmanager
    def _local(self, item: CatalogueItem, scratch: Path) -> Iterator[Path | None]:
        """The item's file on disk: an iCloud original as it is, a Takeout entry copied."""
        if item.original_path is None:
            yield None
        elif item.source == "icloud":
            path = None if self.library is None else self.library / item.original_path
            yield path if path is not None and path.is_file() else None
        elif self.takeout_dir is None:
            yield None
        else:
            if self._takeout is None:
                self._takeout = open_takeout(self.takeout_dir)
                self._entries = {self._takeout.locator(e): e for e in self._takeout.entries}
            entry = self._entries.get(item.original_path)
            if entry is None:
                yield None
            else:
                with self._takeout.local_copy(entry, scratch) as (copy, _):
                    yield copy
