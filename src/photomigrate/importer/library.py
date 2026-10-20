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


class PhotosLibrary(Protocol):
    def import_assets(self, assets: Sequence[Sequence[Path]], album: str) -> list[str]:
        """Import each asset (one file, or a Live Photo's still and clip) into ``album``,
        created if needed. Returns each asset's uuid, in order."""
        ...

    def add_to_album(self, album: str, uuids: Sequence[str]) -> None: ...

    def remove_from_album(self, album: str, uuids: Sequence[str]) -> None:
        """Take assets out of an album; they stay in the library."""
        ...

    def set_details(
        self, uuid: str, *, favorite: bool, description: str | None, keywords: Sequence[str]
    ) -> None:
        """Set the favourite, the caption (if given), and add ``keywords`` to the asset's."""
        ...

    def set_date(self, uuid: str, when: datetime) -> None: ...

    def set_location(self, uuid: str, latitude: float, longitude: float) -> None: ...


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
        if opened is None or os.path.realpath(opened) != os.path.realpath(library):
            raise LibraryMismatchError(
                f"Photos has {opened} open, not {library}: open that library in Photos first"
            )
        self._script = _photoscript()
        self._photos: Any = self._script.PhotosLibrary()

    def _album(self, name: str) -> Any:
        return self._photos.album(name) or self._photos.create_album(name)

    def import_assets(self, assets: Sequence[Sequence[Path]], album: str) -> list[str]:
        paths = [str(path) for asset in assets for path in asset]
        imported = self._photos.import_photos(
            paths, album=self._album(album), skip_duplicate_check=True
        )
        by_name = {photo.filename: photo.uuid for photo in imported}
        uuids = []
        for asset in assets:
            name = Path(asset[0]).name
            if name not in by_name:
                raise RuntimeError(f"Photos did not import {name}")
            uuids.append(by_name[name])
        return uuids

    def add_to_album(self, album: str, uuids: Sequence[str]) -> None:
        self._album(album).add([self._script.Photo(uuid) for uuid in uuids])

    def remove_from_album(self, album: str, uuids: Sequence[str]) -> None:
        found = self._photos.album(album)
        if found is not None:
            found.remove([self._script.Photo(uuid) for uuid in uuids])

    def set_details(
        self, uuid: str, *, favorite: bool, description: str | None, keywords: Sequence[str]
    ) -> None:
        photo = self._script.Photo(uuid)
        photo.favorite = favorite
        if description is not None:
            photo.description = description
        photo.keywords = sorted(set(photo.keywords) | set(keywords))

    def set_date(self, uuid: str, when: datetime) -> None:
        local = when.astimezone().replace(tzinfo=None) if when.tzinfo else when
        self._script.Photo(uuid).date = local

    def set_location(self, uuid: str, latitude: float, longitude: float) -> None:
        self._script.Photo(uuid).location = (latitude, longitude)
