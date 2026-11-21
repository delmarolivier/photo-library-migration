"""One-off recovery for the 2026-11-09 ``import --apply`` crash on batch-032652.

That batch's single AppleScript import call (5,195 items) ran past Photos' AppleEvent
timeout. The harness killed and retried Photos, but Photos had already physically imported
some of the batch into "From Google" before the kill; the retry also failed, so the whole
action's exception rolled back its catalogue transaction. Those already-imported assets are
real, correct photos in the live library with no catalogue ``imports`` record and none of the
post-import metadata (favourite, keywords, date/location fallback, Google albums) applied.

This finds them by the working copy's "{item_id}-{name}" filename convention, replays exactly
the metadata step ``ImportStage._import`` would have run (via its own ``_working_copy`` and
``_details`` helpers, so there is no reimplemented logic to get subtly wrong), and records
them as imported so a future ``plan()`` does not try to import them again and duplicate them.
Nothing is deleted or re-imported; this only ever adds catalogue rows and sets metadata on
assets that already exist. Regenerated working copies live in a ``TemporaryDirectory``, which
cleans itself up, same as a normal import batch's scratch folder.

Usage: dry run (prints what it would do) by default; ``--apply`` to actually write.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

from photomigrate.catalogue import Catalogue
from photomigrate.config import ImportConfig
from photomigrate.importer.library import PhotoscriptLibrary
from photomigrate.importer.stage import FROM_GOOGLE, ImportStage
from photomigrate.ingest.policy import local_zone

CATALOGUE = Path("/Volumes/Photos/photomigrate/catalogue.sqlite")
PLAN = Path("/Volumes/Photos/photomigrate/plans/import-20261109T115309Z-apply.json")
LIBRARY = Path("/Volumes/Photos/Pictures/Photos Library.photoslibrary")
TAKEOUT = Path("/Volumes/Photos/takeout")
WORK_DIR = Path("/Volumes/Photos/photomigrate/work")
BATCH_TARGET = "batch-032652"

NAME_PREFIX = re.compile(r"^(\d+)-")

Pair = tuple[int, str, int | None, str | None]  # still_id, uuid, clip_id, clip_uuid


def _find_recoverable(catalogue: Catalogue, by_item_id: dict[int, str], pairs: list) -> list[Pair]:
    tracked = {uuid for uuid, _, _ in catalogue.imports().values()}
    recovered: list[Pair] = []
    for still_key, clip_key in pairs:
        still = catalogue.get_item("google", still_key)
        if still is None or still.id not in by_item_id:
            continue
        uuid = by_item_id[still.id]
        if uuid in tracked:
            continue
        clip_id = clip_uuid = None
        if clip_key is not None:
            clip = catalogue.get_item("google", clip_key)
            if clip is not None:
                clip_id = clip.id
                # Photos joins a still+clip into one asset (shares the still's uuid) or keeps
                # them apart (the clip gets its own album entry); either way is recorded,
                # matching PhotoscriptLibrary.import_assets' own fallback.
                clip_uuid = by_item_id.get(clip_id, uuid)
        recovered.append((still.id, uuid, clip_id, clip_uuid))
    return recovered


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the recovery (else dry run)")
    args = parser.parse_args()

    with open(PLAN) as f:
        plan = json.load(f)
    batch = next(a for a in plan["actions"] if a["target"] == BATCH_TARGET)
    pairs: list[list[str | None]] = batch["details"]["assets"]

    library = PhotoscriptLibrary(LIBRARY)
    album = library._photos.album(FROM_GOOGLE)
    if album is None:
        raise SystemExit(f'album "{FROM_GOOGLE}" does not exist')
    by_item_id: dict[int, str] = {}
    for photo in album.photos():
        match = NAME_PREFIX.match(photo.filename)
        if match:
            by_item_id[int(match.group(1))] = photo.uuid
    print(f'{len(by_item_id)} name-parsed members in "{FROM_GOOGLE}"')

    with Catalogue.open(CATALOGUE) as catalogue:
        recovered = _find_recoverable(catalogue, by_item_id, pairs)
        print(
            f"{len(recovered)} of {len(pairs)} batch pairs recovered (already in Photos, untracked)"
        )
        print(
            f"{len(pairs) - len(recovered)} not yet imported (next run will import them normally)"
        )
        for still_id, uuid, clip_id, clip_uuid in recovered[:5]:
            tail = f", clip {clip_id} -> {clip_uuid}" if clip_id else ""
            print(f"  sample: item {still_id} -> {uuid}{tail}")

        if not args.apply:
            print("\nDry run: nothing was changed. Re-run with --apply to record this.")
            return

        with (
            ImportStage(
                library, TAKEOUT, WORK_DIR, zone=local_zone(), config=ImportConfig()
            ) as stage,
            tempfile.TemporaryDirectory(dir=WORK_DIR, prefix="recover-") as scratch_dir,
            catalogue.transaction(),
        ):
            scratch = Path(scratch_dir)
            for still_id, uuid, clip_id, clip_uuid in recovered:
                still_item = catalogue.item(still_id)
                path, unwritten = stage._working_copy(catalogue, still_item, scratch)
                catalogue.record_import(still_id, uuid, _sha256(path), BATCH_TARGET)
                stage._details(catalogue, still_item, uuid, unwritten)
                if clip_id is not None:
                    clip_item = catalogue.item(clip_id)
                    clip_path, _ = stage._working_copy(catalogue, clip_item, scratch)
                    catalogue.record_import(clip_id, clip_uuid, _sha256(clip_path), BATCH_TARGET)
            print(f"Recorded {len(recovered)} items as imported.")


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
