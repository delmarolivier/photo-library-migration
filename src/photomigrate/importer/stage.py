"""``photomigrate import``: approved Google items into iCloud Photos, and fixes to existing items.

Plan (a dry run unless ``--apply``):

* ``import-batch``: the Google items whose outcome is ``import`` and that are not imported
  yet, a Live Photo's still and clip as one asset, in batches of ``[import] batch_gigabytes``
  of originals.
* ``set-date`` and ``set-location``: answered dates and approved places for existing iCloud
  items (Photos changes them in place, like osxphotos timewarp).
* ``stage-to-delete`` and ``unstage-to-delete``: keep the album "To Delete" exactly the
  existing iCloud items the review rejected (``outcomes.to_delete``). Delmar deletes them by
  hand; taking an asset out of an album never deletes it.

Applying a batch streams each original out of its zip into a scratch folder, writes the
working copy with exiftool (``-o``: the original is only read) using ``planned_write`` plus
an approved place, imports the batch into the album "From Google", then sets favourites,
captions and keywords (``People/…``, ``date-approx``, ``location-inferred``, ``from-google``),
adds each asset to its Google albums, and records the import (Photos uuid, working copy
SHA-256). The scratch folder goes with the batch. Recorded items are never imported again.
"""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from datetime import datetime, tzinfo
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self, cast

from photomigrate.analyse.date_fixes import planned_write
from photomigrate.analyse.locations import INFERRED_KEYWORD, approved_location
from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.config import ImportConfig
from photomigrate.importer.library import PhotosLibrary
from photomigrate.importer.outcomes import outcomes, to_delete
from photomigrate.ingest.exif import ExifTool, MetadataWrite, can_write
from photomigrate.ingest.takeout import Entry, Takeout, open_takeout
from photomigrate.runner import Action, JSONValue

FROM_GOOGLE = "From Google"
TO_DELETE = "To Delete"
FROM_GOOGLE_KEYWORD = "from-google"
_GIGABYTE = 1_000_000_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class ImportStage:
    name = "import"

    def __init__(
        self,
        library: PhotosLibrary | None,
        takeout_dir: Path,
        work_dir: Path,
        *,
        zone: tzinfo,
        config: ImportConfig,
    ) -> None:
        """``library`` may be ``None`` for a dry run, which never touches Photos."""
        self.library = library
        self.takeout_dir = takeout_dir
        self.work_dir = work_dir
        self.zone = zone
        self.config = config
        self._takeout: Takeout | None = None
        self._entries: dict[str, Entry] = {}
        self._exiftool: ExifTool | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._exiftool is not None:
            self._exiftool.close()
        if self._takeout is not None:
            self._takeout.close()

    @property
    def takeout(self) -> Takeout:
        if self._takeout is None:
            self._takeout = open_takeout(self.takeout_dir)
            self._entries = {self._takeout.locator(e): e for e in self._takeout.entries}
        return self._takeout

    def _entry(self, item: CatalogueItem) -> Entry:
        self.takeout  # noqa: B018  (opens the Takeout and indexes its entries)
        return self._entries[cast(str, item.original_path)]

    # -- plan -------------------------------------------------------------------------------

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        found = outcomes(catalogue)
        done = catalogue.imports()
        clips = {o.partner: o.item_id for o in found.values() if o.status == "follows"}
        limit = self.config.batch_gigabytes * _GIGABYTE
        batch: list[JSONValue] = []
        size, first = 0, 0
        for item in catalogue.items("google"):
            item_id = cast(int, item.id)
            if found[item_id].status != "import" or item_id in done:
                continue
            clip = catalogue.item(clips[item_id]) if item_id in clips else None
            members = [item] if clip is None else [item, clip]
            weight = sum(self._entry(m).size for m in members)
            if batch and size + weight > limit:
                yield self._batch(first, batch, size)
                batch, size = [], 0
            if not batch:
                first = item_id
            batch.append([item.source_key, None if clip is None else clip.source_key])
            size += weight
        if batch:
            yield self._batch(first, batch, size)
        yield from self._fixes(catalogue)
        yield from self._staging(catalogue)

    def _batch(self, first: int, assets: list[JSONValue], size: int) -> Action:
        return Action("import-batch", f"batch-{first:06d}", {"assets": assets, "bytes": size})

    def _fixes(self, catalogue: Catalogue) -> Iterator[Action]:
        for item in catalogue.items("icloud"):
            item_id = cast(int, item.id)
            dates = catalogue.date_candidates(item_id)
            given = dates.get("user")
            if given is not None and given != dates.get("icloud"):
                keywords = sorted(k for k in catalogue.keywords(item_id) if k == "date-approx")
                details: dict[str, JSONValue] = {
                    "to": given.isoformat(),
                    "keywords": list(keywords),
                }
                yield Action("set-date", item.source_key, details)
            place = approved_location(catalogue, item_id)
            if place is not None and place != (item.latitude, item.longitude):
                where: dict[str, JSONValue] = {"latitude": place[0], "longitude": place[1]}
                yield Action("set-location", item.source_key, where)

    def _staging(self, catalogue: Catalogue) -> Iterator[Action]:
        wanted = to_delete(catalogue)
        staged = catalogue.staged()
        for item_id, reason in sorted(wanted.items()):
            if staged.get(item_id) != reason:
                uuid = catalogue.item(item_id).source_key
                yield Action("stage-to-delete", uuid, {"reason": reason})
        for item_id in sorted(staged.keys() - wanted.keys()):
            yield Action("unstage-to-delete", catalogue.item(item_id).source_key)

    # -- apply ------------------------------------------------------------------------------

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        if self.library is None:
            raise RuntimeError("a dry run has no Photos library to import into")
        if action.kind == "set-date":
            self.library.set_date(action.target, datetime.fromisoformat(str(action.details["to"])))
            self._keywords(catalogue, action.target, cast(list[str], action.details["keywords"]))
        elif action.kind in ("stage-to-delete", "unstage-to-delete"):
            item_id = cast(int, catalogue.item_for_key("icloud", action.target))
            if action.kind == "stage-to-delete":
                self.library.add_to_album(TO_DELETE, [action.target])
                catalogue.set_staged(item_id, str(action.details["reason"]))
            else:
                self.library.remove_from_album(TO_DELETE, [action.target])
                catalogue.set_staged(item_id, None)
        elif action.kind == "set-location":
            details = action.details
            latitude, longitude = (
                cast(float, details["latitude"]),
                cast(float, details["longitude"]),
            )
            self.library.set_location(action.target, latitude, longitude)
            self._keywords(catalogue, action.target, [INFERRED_KEYWORD])
        else:
            self._import(action, catalogue)

    def _keywords(self, catalogue: Catalogue, uuid: str, keywords: Sequence[str]) -> None:
        item = catalogue.get_item("icloud", uuid)
        favorite = bool(item and item.favorite)
        cast(PhotosLibrary, self.library).set_details(
            uuid, favorite=favorite, description=None, keywords=keywords
        )

    def _item(self, catalogue: Catalogue, key: str) -> CatalogueItem:
        item = catalogue.get_item("google", key)
        if item is None:
            raise KeyError(f"google:{key} is no longer in the catalogue")
        return item

    def _import(self, action: Action, catalogue: Catalogue) -> None:
        library = cast(PhotosLibrary, self.library)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.work_dir, prefix="import-") as holder:
            scratch = Path(holder)
            prepared = []
            for key, clip_key in cast(list[list[str | None]], action.details["assets"]):
                members = [self._item(catalogue, cast(str, key))]
                if clip_key is not None:
                    members.append(self._item(catalogue, clip_key))
                prepared.append([(m, *self._working_copy(catalogue, m, scratch)) for m in members])
            uuids = library.import_assets(
                [[p for _, p, _ in asset] for asset in prepared], FROM_GOOGLE
            )
            for asset, uuid in zip(prepared, uuids, strict=True):
                main, _, unwritten = asset[0]
                self._details(catalogue, main, uuid, unwritten)
                for member, path, _ in asset:
                    catalogue.record_import(
                        cast(int, member.id), uuid, _sha256(path), action.target
                    )

    def _working_copy(
        self, catalogue: Catalogue, item: CatalogueItem, scratch: Path
    ) -> tuple[Path, MetadataWrite]:
        """The item's working copy, and what of its plan could not be written into it."""
        item_id = cast(int, item.id)
        name = f"{item_id}-{PurePosixPath(item.source_key).name}"
        plan = planned_write(item, catalogue.date_candidates(item_id), self.zone)
        place = approved_location(catalogue, item_id)
        if place is not None:
            plan = dataclasses.replace(plan, latitude=place[0], longitude=place[1], altitude=None)
        dest = scratch / name
        with self.takeout.local_copy(self._entry(item), scratch) as (copy, _):
            if plan and can_write(name):
                if self._exiftool is None:
                    self._exiftool = ExifTool()
                self._exiftool.write(copy, dest, plan)
                return dest, MetadataWrite()
            shutil.copyfile(copy, dest)
        return dest, plan

    def _details(
        self, catalogue: Catalogue, item: CatalogueItem, uuid: str, unwritten: MetadataWrite
    ) -> None:
        library = cast(PhotosLibrary, self.library)
        item_id = cast(int, item.id)
        keywords = set(catalogue.keywords(item_id)) | {FROM_GOOGLE_KEYWORD}
        if approved_location(catalogue, item_id) is not None:
            keywords.add(INFERRED_KEYWORD)
        library.set_details(
            uuid, favorite=item.favorite, description=item.description, keywords=sorted(keywords)
        )
        if unwritten.taken is not None:
            library.set_date(uuid, unwritten.taken)
        if unwritten.latitude is not None and unwritten.longitude is not None:
            library.set_location(uuid, unwritten.latitude, unwritten.longitude)
        for title in catalogue.albums_of(item_id):
            library.add_to_album(title, [uuid])
