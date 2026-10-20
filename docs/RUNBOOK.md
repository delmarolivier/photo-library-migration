# Runbook: the real migration

This is for Delmar's MacBook Air ("AirDelmar"), reached with `ssh openclaw`. Every step that
writes is a dry run first. **Before each `--apply` there is a GO checkpoint**: run the dry run,
read its plan, and only continue once Delmar says "go". Nothing in this tool deletes anything.
Removals end up in the "To Delete" album, and Delmar empties it by hand at the end.

Paths below assume the external APFS drive is mounted as `/Volumes/Photos`. Change them if
the drive has another name.

```sh
ssh openclaw
cd ~/Code/photo-library-migration && git pull && uv sync
tmux new -s photomigrate        # long steps survive a dropped connection: tmux attach -t photomigrate
```

## 0. Before anything

- [ ] The external drive is attached, formatted APFS, with at least 2 TB free:
      `df -h /Volumes/Photos`.
- [ ] All 12 Takeout parts are in `/Volumes/Photos/takeout/` (`takeout-*.zip`). The sizes
      match the download page, and `unzip -t` passes on each part.
- [ ] The Photos library has been moved to the drive and is the System Photo Library
      (Photos > Settings > General). It is `/Volumes/Photos/Photos Library.photoslibrary`.
- [ ] Photos > Settings > iCloud > **Download Originals to this Mac** is on, and Photos has
      finished downloading (no progress at the bottom of the Library view).

## 1. Backup

- [ ] A Time Machine backup that includes the library has finished since the last change.
      Check it in Time Machine: the library is there, with today's date.
- [ ] Record it: `uv run photomigrate confirm-backup`. This writes `data/BACKUP-CONFIRMED`;
      `import --apply` refuses to run without it.

## 2. Permissions

- [ ] **Full Disk Access** (System Settings > Privacy & Security > Full Disk Access) for
      `sshd-keygen-wrapper` (for commands run over ssh) and for the terminal app.
- [ ] **Automation**: the first `import --apply` asks to let the terminal control Photos. Be
      at the Air's screen to allow it, or allow it beforehand under Privacy & Security >
      Automation.
- [ ] `.claude/settings.json` denies `~/Pictures`. The library now lives on the external
      drive, so that rule can stay. Only lift it if the library is still under
      `~/Pictures`, and put it back afterwards.
- [ ] Optional, once: the real Photos.app test (see docs/PROGRESS.md). Make an empty library
      under /tmp, open it in Photos, run the test, then switch Photos back to the real
      library.

## 3. Rehearsal on the one-album sample

The sample is the small Takeout used during development (one shared album, about
3,300 items, 4 GB). Copy it to `/Volumes/Photos/sample/`. The rehearsal uses its own
catalogue, so the full run starts clean:

```sh
export PHOTOMIGRATE_CATALOGUE=data/rehearsal.sqlite PHOTOMIGRATE_PLAN_DIR=data/rehearsal-plans
```

What to expect, measured on this sample on the work Mac (2026-10-21; without the library):
3,308 Google items. Before the review, 2,156 can be imported (one 3 GB batch). The review asks
about 258 duplicate groups, 128 best-shot groups, 152 clutter items (138 Google creations: one
bulk answer), 29 quality flags, 1 date batch and 1 unmatched item. The 1,562 edit pairs follow
the policy (keep both) unless one bulk answer changes it.

1. **Ingest** (reads the zips, writes only the catalogue):
   `uv run photomigrate ingest /Volumes/Photos/sample`. Read the plan.
   **GO** → `uv run photomigrate ingest /Volumes/Photos/sample --apply`
2. **Index the library** (read-only; quit Photos first):
   `uv run photomigrate index "/Volumes/Photos/Photos Library.photoslibrary"`. Check the
   warning about originals not on this Mac: it should list none.
   **GO** → the same command with `--apply`.
3. **Analyse** (catalogue only): `uv run photomigrate analyse`, then **GO** →
   `uv run photomigrate analyse --apply`. Add `--timeline /Volumes/Photos/Timeline.json` to
   use the Maps Timeline for 2025–26 photos.
4. **Review**: start the gallery on the Air, and reach it from the work Mac through an SSH
   tunnel (it only listens on 127.0.0.1):
   ```sh
   uv run photomigrate review --takeout /Volumes/Photos/sample \
       --library "/Volumes/Photos/Photos Library.photoslibrary"
   # on the work Mac:  ssh -N -L 8765:127.0.0.1:8765 openclaw   then open http://127.0.0.1:8765
   ```
   Answer every queue. Google creations need a yes or a no; best-shot, duplicates and dates
   need their groups answered. `report` lists anything left.
5. **Import**: open Photos with the real library. Dry run first:
   `uv run photomigrate import --takeout /Volumes/Photos/sample`. Read the plan: batches,
   date and location fixes, and To Delete staging.
   **GO** → `uv run photomigrate import --takeout /Volumes/Photos/sample --library "/Volumes/Photos/Photos Library.photoslibrary" --apply`.
   It asks once more before importing.
6. **Report**: `uv run photomigrate report --out data/rehearsal-report`. It must say
   **0 unaccounted** (exit 0). Open `data/rehearsal-report/report.html`.
7. Post the report's totals on Linear (CLA-92).
8. Back to the default catalogue for the full run:
   `unset PHOTOMIGRATE_CATALOGUE PHOTOMIGRATE_PLAN_DIR`.

## 4. Delmar checks the rehearsal in Photos

- [ ] "From Google" holds the imported items, and the shared album is recreated with its
      members.
- [ ] Favourites, captions, `People/…` keywords and `from-google` are there. Live Photos
      play.
- [ ] Dates look right; `date-approx` items are on 1 July of their year.
- [ ] "To Delete" holds only what was rejected in review, and nothing has disappeared.

Fix anything odd before the full run. The rehearsal's imports are now ordinary iCloud items.
The full run indexes them again and proposes their Google copies as duplicates.

## 5. The full run

Same steps, default catalogue, all 12 parts. Ingest and index take hours, so run them in
tmux.

1. `uv run photomigrate ingest /Volumes/Photos/takeout` → **GO** → `--apply`.
2. Quit Photos. `uv run photomigrate index "/Volumes/Photos/Photos Library.photoslibrary"` →
   **GO** → `--apply`.
3. `uv run photomigrate analyse [--timeline …]` → **GO** → `--apply`.
4. `uv run photomigrate review --takeout /Volumes/Photos/takeout --library …`. Answer every
   queue; it can take several sittings, since answers are saved as you go.
5. `uv run photomigrate import --takeout /Volumes/Photos/takeout` → **GO** →
   `--library … --apply`. It imports in batches of `[import] batch_gigabytes` (20). Before
   each sitting, check the free space on the drive and the internal disk (`df -h`). If it
   stops, run the same command again: imported items are skipped.
6. After the import, quit Photos and index again (`--apply`), so the catalogue knows the new
   assets.

## 6. Final report

- [ ] `uv run photomigrate report` says **0 unaccounted** and exits 0. Keep
      `data/report/report.html` and `report.csv`.
- [ ] Post the totals on Linear.

## 7. Cleanup (Delmar, by hand)

- [ ] Go through the "To Delete" album in Photos, then delete its contents yourself. The
      tool never does.
- [ ] Keep the Takeout zips for 30 days after the final report, then remove them by hand.

## If something goes wrong

- **"Photos has … open, not …"**: Photos has another library open. Open the right one
  (hold Option when launching Photos) and run the command again.
- **"no confirmed backup"**: do step 1.
- **A command stops partway**: run it again. Every stage is resumable, and finished work
  isn't redone.
- **Originals not on this Mac** (from index): let Photos download them (step 0), then index
  again.
- **Anything unexpected in a plan**: don't say go. Post the plan file (`data/plans/…`) on
  Linear.
