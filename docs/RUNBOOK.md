# Runbook: the real migration

This is for Delmar's MacBook Air ("AirDelmar"), reached with `ssh openclaw`. Every step that
writes is a dry run first. **Before each `--apply` there is a GO checkpoint**: run the dry run,
read its plan, and only continue once Delmar says "go". Nothing in this tool deletes anything.
Removals end up in the "To Delete" album, and Delmar empties it by hand at the end.

Paths below assume the external APFS drive is called "Photos", so it mounts as
`/Volumes/Photos`. Everything big lives on it: the Takeout, the library, and the tool's own
data (catalogue, plans, working copies, thumbnails, reports) in `/Volumes/Photos/photomigrate/`.
The internal disk has only about 70 GB free.

```sh
ssh openclaw
cd ~/Code/photo-library-migration && git pull && uv sync
tmux new -s photomigrate        # long steps survive a dropped connection: tmux attach -t photomigrate
# in every new shell (the rehearsal and the full run share this one catalogue):
export PHOTOMIGRATE_CATALOGUE=/Volumes/Photos/photomigrate/catalogue.sqlite
export PHOTOMIGRATE_PLAN_DIR=/Volumes/Photos/photomigrate/plans
W=/Volumes/Photos/photomigrate
L="/Volumes/Photos/Photos Library.photoslibrary"
```

Use `"$L"` with its quotes: the library's name has spaces.

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

A one-off full copy of the library is enough; Time Machine is not needed. The import adds
items and changes some existing ones (dates, places, keywords), and iCloud syncs every change
to all devices, so iCloud is not a backup. The copy is what makes a bad run undoable: quit
Photos and put the copy back.

- [ ] Quit Photos. Copy `/Volumes/Photos/Photos Library.photoslibrary` in the Finder, ideally
      to another disk (`/Volumes/Photos/backup/` also works; it protects against a bad
      import, but not against the drive failing). Do this after the move (step 0) and before
      the rehearsal's import.
- [ ] Check the copy opens: hold Option while launching Photos, choose the copy, look around,
      quit, and switch back to the real library the same way.
- [ ] Record it: `uv run photomigrate confirm-backup`. This writes `data/BACKUP-CONFIRMED`;
      `import --apply` refuses to run without it.
- [ ] Make a fresh copy before the full run's import too (step 5), after the rehearsal's
      import, and run `confirm-backup` again.

## 2. Permissions

- [ ] **Full Disk Access** (System Settings > Privacy & Security > Full Disk Access) for
      `sshd-keygen-wrapper` (for commands run over ssh) and for the terminal app. Anything
      run over ssh as you can then read all your files; step 7 takes it back.
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
3,300 items, 4 GB). Copy it to `/Volumes/Photos/sample/`. The rehearsal and the full run
share one catalogue. The full Takeout holds the same album files (same paths, SHA-256 and
Google URLs), so ingest recognises them later as the items already imported, and the
rehearsal's review answers carry over.

What to expect, measured on this sample on the work Mac (2026-10-21; without the library):
3,308 Google items. Before the review, 2,156 can be imported (one 3 GB batch). The review asks
about 258 duplicate groups, 128 best-shot groups, 152 clutter items (138 Google creations: one
bulk answer), 29 quality flags, 1 date batch and 1 unmatched item. The 1,562 edit pairs follow
the policy (keep both) unless one bulk answer changes it.

1. **Ingest** (reads the zips, writes only the catalogue):
   `uv run photomigrate ingest /Volumes/Photos/sample --work-dir $W/work`. Read the plan.
   **GO** → the same command with `--apply`.
2. **Index the library** (read-only; quit Photos first): `uv run photomigrate index "$L"`.
   Check the warning about originals not on this Mac: it should list none.
   **GO** → `uv run photomigrate index "$L" --apply`.
3. **Analyse** (catalogue only): `uv run photomigrate analyse`, then **GO** →
   `uv run photomigrate analyse --apply`. Add `--timeline /Volumes/Photos/Timeline.json` to
   use the Maps Timeline for 2025–26 photos.
4. **Review**: start the gallery on the Air, and reach it from the work Mac through an SSH
   tunnel (it only listens on 127.0.0.1):
   ```sh
   uv run photomigrate review --takeout /Volumes/Photos/sample --library "$L" --thumbs $W/thumbs
   # on the work Mac:  ssh -N -L 8765:127.0.0.1:8765 openclaw   then open http://127.0.0.1:8765
   ```
   Answer every queue. Google creations need a yes or a no; best-shot, duplicates and dates
   need their groups answered. Judge the best-shot groups too: if they look too loose or too
   strict, we change `[best_shot] min_similarity` (0.94) and analyse again. `report` lists
   anything left.
5. **Import**: open Photos with the real library. Dry run first:
   `uv run photomigrate import --takeout /Volumes/Photos/sample --work-dir $W/work`. Read the
   plan: batches, date and location fixes, and To Delete staging.
   **GO** → the same command with `--library "$L" --apply`. It asks once more before
   importing.
6. **Index again** (quit Photos): `uv run photomigrate index "$L" --apply`. The catalogue now
   knows the imported assets. They are recognised as the importer's own and kept out of every
   review queue.
7. **Report**: `uv run photomigrate report --out $W/rehearsal-report`. It must say
   **0 unaccounted** (exit 0). Open `$W/rehearsal-report/report.html`.
8. Post the report's totals on Linear (CLA-92).

## 4. Delmar checks the rehearsal in Photos

- [ ] "From Google" holds the imported items. Its count in Photos equals the report's
      "N assets in From Google" (a Live Photo's still and clip are one asset).
- [ ] The shared album is recreated with its members.
- [ ] Favourites, captions, `People/…` keywords and `from-google` are there. Live Photos
      play.
- [ ] Dates look right; `date-approx` items are on 1 July of their year.
- [ ] "To Delete" holds only what was rejected in review, and nothing has disappeared.
- [ ] On the iPhone, once iCloud has synced (it can take a while for thousands of items):
      "From Google" and the shared album are there with the same counts.

Fix anything odd before the full run.

## 5. The full run

Same steps, same catalogue, all 12 parts. Ingest and index take hours, so run them in tmux.

1. `uv run photomigrate ingest /Volumes/Photos/takeout --work-dir $W/work` → **GO** →
   `--apply`.
2. Quit Photos. `uv run photomigrate index "$L"` → **GO** → `--apply`.
3. `uv run photomigrate analyse [--timeline …]` → **GO** → `--apply`.
4. `uv run photomigrate review --takeout /Volumes/Photos/takeout --library "$L" --thumbs $W/thumbs`.
   Answer every queue; it can take several sittings, since answers are saved as you go.
5. Make a fresh copy of the library and run `confirm-backup` again (step 1).
6. `uv run photomigrate import --takeout /Volumes/Photos/takeout --work-dir $W/work` → **GO** →
   the same with `--library "$L" --apply`. It imports in batches of
   `[import] batch_gigabytes` (20). Before each sitting, check the free space on the drive
   (`df -h /Volumes/Photos`). If it stops, run the same command again: imported items are
   skipped.
7. After the import, quit Photos and index again (`--apply`).

## 6. Final report

- [ ] `uv run photomigrate report --out $W/report` says **0 unaccounted** and exits 0. Keep
      `$W/report/report.html` and `report.csv`. Check the "N assets in From Google" count
      against Photos once more.
- [ ] Post the totals on Linear.

## 7. Cleanup (Delmar, by hand)

- [ ] Go through the "To Delete" album in Photos, then delete its contents yourself. The
      tool never does.
- [ ] Keep the Takeout zips for 30 days after the final report, then remove them by hand.
- [ ] Take back the permissions from step 2: remove `sshd-keygen-wrapper` (and the terminal,
      if it was only for this) from Full Disk Access, and the terminal from Automation >
      Photos.

## If something goes wrong

- **"Photos has … open, not …"**: Photos has another library open. Open the right one
  (hold Option when launching Photos) and run the command again.
- **"no confirmed backup"**: do step 1.
- **A bad import or wrong dates**: quit Photos, move the damaged library aside (don't delete
  it), put the copy from step 1 back, and open it.
- **A command stops partway**: run it again. Every stage is resumable, and finished work
  isn't redone.
- **Originals not on this Mac** (from index): let Photos download them (step 0), then index
  again.
- **Anything unexpected in a plan**: don't say go. Post the plan file (`$W/plans/…`) on
  Linear.
