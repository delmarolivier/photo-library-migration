"""The Photos library the importer writes to (macOS: photoscript, which drives Photos.app).

Photos.app works on whichever library it has open, so ``PhotoscriptLibrary`` first checks
that Photos has the expected library open (``LibraryMismatchError`` otherwise): an import can
never land in the wrong library. Imports skip Photos' duplicate dialog (the catalogue already
decided what is a duplicate) and map each imported asset back by its file name, which the
importer makes unique. A Live Photo is imported as its still and clip together; Photos joins
them into one asset.

Nothing here deletes anything; the "To Delete" album is an album like any other.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol


class LibraryMismatchError(RuntimeError):
    """Photos has another library open than the one given."""


class PhotoNotFoundError(RuntimeError):
    """Photos' own scripting dictionary doesn't recognise this uuid, despite a database row for
    it (seen for syndicated Shared-with-You items from Messages: a lightweight record, not a
    real, scriptable library asset)."""


class PhotosLibrary(Protocol):
    def import_assets(
        self, assets: Sequence[Sequence[Path]], album: str
    ) -> list[tuple[str, ...] | None]:
        """Import each asset (one file, or a Live Photo's still and clip) into ``album``,
        created if needed. Returns the uuid of each file, asset by asset: a still and its clip
        share one when Photos joins them into a Live Photo, and get two when it does not
        (files without matching Apple content identifiers). ``None`` where Photos silently
        left an asset out of a batch it otherwise imported."""
        ...

    def add_to_album(self, album: str, uuids: Sequence[str]) -> None:
        """Raises ``PhotoNotFoundError`` for a uuid Photos' own scripting dictionary doesn't
        recognise."""
        ...

    def remove_from_album(self, album: str, uuids: Sequence[str]) -> None:
        """Take assets out of an album; they stay in the library. Raises ``PhotoNotFoundError``
        as ``add_to_album`` does."""
        ...

    def set_details(
        self, uuid: str, *, favorite: bool, description: str | None, keywords: Sequence[str]
    ) -> None:
        """Set the favourite, the caption (if given), and add ``keywords`` to the asset's.
        Raises ``PhotoNotFoundError`` as ``add_to_album`` does."""
        ...

    def set_date(self, uuid: str, when: datetime) -> None:
        """Raises ``PhotoNotFoundError`` as ``add_to_album`` does."""
        ...

    def set_location(self, uuid: str, latitude: float, longitude: float) -> None:
        """Raises ``PhotoNotFoundError`` as ``add_to_album`` does."""
        ...


def _photoscript() -> ModuleType:
    import photoscript

    module: ModuleType = photoscript
    return module


def _open_library() -> str | None:
    """The library Photos has open (its last one), from Photos' preferences."""
    import osxphotos.utils

    found: str | None = osxphotos.utils.get_last_library_path()
    return found


class PhotoscriptLibrary:
    def __init__(self, library: Path) -> None:
        opened = _open_library()
        if opened is None:
            raise LibraryMismatchError(
                "cannot tell which library Photos has open: its settings are unreadable. Give "
                "this terminal Full Disk Access (System Settings > Privacy & Security), restart "
                "it, and try again"
            )
        if os.path.realpath(opened) != os.path.realpath(library):
            raise LibraryMismatchError(
                f"Photos has {opened} open, not {library}: open that library in Photos first"
            )
        self._script = _photoscript()
        self._photos: Any = self._script.PhotosLibrary()

    def _album(self, name: str) -> Any:
        return self._photos.album(name) or self._photos.create_album(name)

    def _photo(self, uuid: str) -> Any:
        try:
            return self._script.Photo(uuid)
        except ValueError as exc:
            raise PhotoNotFoundError(str(exc)) from exc

    def import_assets(
        self, assets: Sequence[Sequence[Path]], album: str
    ) -> list[tuple[str, ...] | None]:
        paths = [str(path) for asset in assets for path in asset]
        imported = self._photos.import_photos(
            paths, album=self._album(album), skip_duplicate_check=True
        )
        by_name = {photo.filename: photo.uuid for photo in imported}
        uuids: list[tuple[str, ...] | None] = []
        for asset in assets:
            name = Path(asset[0]).name
            if name not in by_name:
                uuids.append(None)
                continue
            # a clip Photos joined to its still has no asset of its own
            uuids.append(tuple(by_name.get(Path(p).name, by_name[name]) for p in asset))
        return uuids

    def add_to_album(self, album: str, uuids: Sequence[str]) -> None:
        self._album(album).add([self._photo(uuid) for uuid in uuids])

    def remove_from_album(self, album: str, uuids: Sequence[str]) -> None:
        found = self._photos.album(album)
        if found is not None:
            found.remove([self._photo(uuid) for uuid in uuids])

    def set_details(
        self, uuid: str, *, favorite: bool, description: str | None, keywords: Sequence[str]
    ) -> None:
        photo = self._photo(uuid)
        photo.favorite = favorite
        if description is not None:
            photo.description = description
        photo.keywords = sorted(set(photo.keywords) | set(keywords))

    def set_date(self, uuid: str, when: datetime) -> None:
        local = when.astimezone().replace(tzinfo=None) if when.tzinfo else when
        self._photo(uuid).date = local

    def set_location(self, uuid: str, latitude: float, longitude: float) -> None:
        self._photo(uuid).location = (latitude, longitude)
