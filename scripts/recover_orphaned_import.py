"""Recovery for an ``import --apply`` batch that landed in Photos with no catalogue record.

Two known causes so far, both leaving the same symptom: a batch's single AppleScript import
call lands some or all of its assets in "From Google" for real, but something later in the
same action raises before the action's catalogue transaction commits, so the rollback erases
every ``imports`` row the action would have written — the Photos-side effect can't be rolled
back with it. 2026-11-09 (batch-032652, 5,195 items): the import call itself outlived Photos'
AppleEvent timeout. 2026-11-21 (batch-032652 again, a fresh 500-item chunk): the import call
succeeded, but a later per-item ``_details`` call (setting keywords) hit a transient
"User canceled" AppleScript error after a timeout-triggered Photos relaunch, which aborted the
whole batch's transaction, not just that one item.

This finds the orphans by the working copy's "{item_id}-{name}" filename convention, replays
exactly the metadata step ``ImportStage._import`` would have run (via its own
``_working_copy`` and ``_details`` helpers, so there is no reimplemented logic to get subtly
wrong), and records them as imported so a future ``plan()`` does not try to import them again
and duplicate them. Nothing is deleted or re-imported; this only ever adds catalogue rows and
sets metadata on assets that already exist. Regenerated working copies live in a
``TemporaryDirectory``, which cleans itself up, same as a normal import batch's scratch
folder. Each item is recorded in its own catalogue transaction, not one for the whole run, so
a failure on one item (the same "_details can raise" risk this script exists to clean up
after) can't erase items this same run already recovered.

Usage: dry run (prints what it would do) by default; ``--apply`` to actually write. ``--plan``
and ``--batch-target`` pick which batch to recover (default: the 2026-11-21 incident).
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

from photomigrate.catalogue import Catalogue
from photomigrate.config import ImportConfig
from photomigrate.importer.library import PhotoNotFoundError, PhotoscriptLibrary
from photomigrate.importer.stage import FROM_GOOGLE, ImportStage
from photomigrate.ingest.policy import local_zone

CATALOGUE = Path("/Volumes/Photos/photomigrate/catalogue.sqlite")
DEFAULT_PLAN = Path("/Volumes/Photos/photomigrate/plans/import-20261121T144012Z-apply.json")
LIBRARY = Path("/Volumes/Photos/Pictures/Photos Library.photoslibrary")
TAKEOUT = Path("/Volumes/Photos/takeout")
WORK_DIR = Path("/Volumes/Photos/photomigrate/work")
DEFAULT_BATCH_TARGET = "batch-032652"

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
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN, help="the apply plan's JSON")
    parser.add_argument("--batch-target", default=DEFAULT_BATCH_TARGET, help="action target")
    args = parser.parse_args()
    batch_target: str = args.batch_target

    with open(args.plan) as f:
        plan = json.load(f)
    batch = next(a for a in plan["actions"] if a["target"] == batch_target)
    pairs: list[list[str | None]] = batch["details"]["assets"]

    # Reading the album's membership through osxphotos (direct, read-only database access)
    # rather than photoscript's live AppleScript enumeration: the latter has been seen to
    # choke on a stale media-item reference ("Can't get media item id ... of album id ...",
    # -1728) after repeated import/kill/relaunch cycles, for an album with thousands of
    # members. Only the actual writes below need AppleScript; this scan does not.
    import osxphotos

    db = osxphotos.PhotosDB(str(LIBRARY))
    album_info = next((a for a in db.album_info if a.title == FROM_GOOGLE), None)
    if album_info is None:
        raise SystemExit(f'album "{FROM_GOOGLE}" does not exist')
    by_item_id: dict[int, str] = {}
    for photo in album_info.photos:
        match = NAME_PREFIX.match(photo.original_filename or "")
        if match:
            by_item_id[int(match.group(1))] = photo.uuid
    print(f'{len(by_item_id)} name-parsed members in "{FROM_GOOGLE}"')

    library = PhotoscriptLibrary(LIBRARY)

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
        ):
            scratch = Path(scratch_dir)
            done = 0
            failed = []
            unaddressable = []
            for still_id, uuid, clip_id, clip_uuid in recovered:
                try:
                    with catalogue.transaction():
                        still_item = catalogue.item(still_id)
                        path, unwritten = stage._working_copy(catalogue, still_item, scratch)
                        catalogue.record_import(still_id, uuid, _sha256(path), batch_target)
                        try:
                            stage._details(catalogue, still_item, uuid, unwritten)
                        except PhotoNotFoundError as exc:
                            # Genuinely imported (its row, path and album entry are all real),
                            # but Photos' scripting dictionary won't address it yet - same
                            # symptom as a syndicated Shared-with-You item. Recording the
                            # import anyway (already done above) is what matters: it stops a
                            # future plan() from re-importing, and duplicating, this asset.
                            # Its metadata is left unset, same as that earlier, ≤5-item case.
                            unaddressable.append((still_id, str(exc)))
                        if clip_id is not None:
                            clip_item = catalogue.item(clip_id)
                            clip_path, _ = stage._working_copy(catalogue, clip_item, scratch)
                            catalogue.record_import(
                                clip_id, clip_uuid, _sha256(clip_path), batch_target
                            )
                except Exception as exc:
                    failed.append((still_id, str(exc)))
                    continue
                done += 1
            print(f"Recorded {done} of {len(recovered)} items as imported.")
            if unaddressable:
                print(f"{len(unaddressable)} item(s) recorded but left with no metadata set:")
                for still_id, message in unaddressable:
                    print(f"  item {still_id}: {message}")
            if failed:
                print(f"{len(failed)} item(s) failed and were NOT recorded (rerun to retry):")
                for still_id, message in failed:
                    print(f"  item {still_id}: {message}")


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
