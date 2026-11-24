# Progress log

## Current state
- Milestone: 4. The real migration (CLA-92), running on the Air itself.
- Done (2026-10-27/28): the 46 Takeout parts (three archives, 444 GB) are on the drive
  `/Volumes/Photos` (plain APFS, 1.8 TB), each zip-tested with its `.sha256`. `ingest --apply`
  done on the full Takeout (150,714 items, 0 failures), then `analyse --apply` on the Google
  items alone (Delmar asked to go ahead without a GO for the Google-only steps).
- Before the iCloud run: shut down the UTM VM (~3.7 GB) and OrbStack, and turn Spotlight off
  for `/Volumes/Photos` (Delmar's choice, 2026-10-28).
- Done (2026-10-29): the review is recorded. Delmar approved a year-by-year proposal (too
  many cards to click), and `photomigrate accept --apply` recorded every unanswered proposal
  as the answer: one version per edit (the edit), quality-flagged shots dropped, dates and
  places left to the gallery. To iCloud: 10,016 photos (9.0 GB) and 1,765 videos (63.3 GB);
  3,086 existing iCloud items go to "To Delete". Catalogue backup from just before:
  `/Volumes/Photos/photomigrate/catalogue-before-accept.sqlite`.
- Done (2026-11-08): library backed up to `/Volumes/Photos/backup/` and `confirm-backup` run.
  `import --apply` on Delmar's GO got through the first batch (1,606 items) then stopped:
  Photos refuses the `.avi` container outright (55 old camcorder clips). Fixed by remuxing
  (falling back to re-encoding) any `.avi` working copy to `.mov` before handoff.
  Also found, before resuming: the failed batch had already imported 703 items into the real
  library that the catalogue never recorded (a partial-batch bug, now fixed too) — reconciled
  (backfilled + finished keywords/albums), verified consistent with the live library. The real
  cause of the stall was never `.avi`: Photos' one AppleScript call per batch has a practical
  limit around 700 items. See the session log below for all of this.
- Done (2026-11-08): resumed with `batch_gigabytes = 2.0`. The original crashed run's
  "Cannot Import" dialog was still sitting unacknowledged in Photos, blocking every
  AppleScript call; it (and 8 more from this session's own failed batches) had to be
  dismissed by hand before any batch could succeed. Then a second, unrelated bug stopped the
  run again: one item (`PeppaCapture11.png`) is actually JPEG bytes under a `.png` name (a
  Picasa-era mislabelling), which `exiftool` refuses to write metadata into — and that refusal
  was propagating uncaught, fatally stopping the whole run rather than just that item. Fixed;
  see the session log below.
- Done (2026-11-08): resumed again; all 13 remaining `import-batch` actions went through (the
  retry's own dialog had already cleared by itself before it needed dismissing). The run then
  hit a third, genuinely different bug on the very first `stage-to-delete` action: Photos'
  AppleScript dictionary refuses a uuid that `osxphotos` finds fine in the database — a
  syndicated Shared-with-You item from Messages (5 total in the whole library), which gets a
  database row but isn't a real, scriptable asset. Fixed: `stage-to-delete`,
  `unstage-to-delete`, `set-date` and `set-location` now warn and skip such an item instead of
  stopping the stage (not retried on a later run — nothing would ever make it succeed; the
  warning is the only record, so these ≤5 items need a manual look if they matter to Delmar).
  See the session log below.
- Done (2026-11-09/21): hit and fixed a crashed-batch variant of the Nov 8 partial-import
  bug (`batch_items` cap + crash recovery in `import_assets`), recovered 1,886 orphaned
  imports, then resumed through a dialog-blocking stall and an 11-day gap of further,
  apparently manual `import --apply` runs.
- Done (2026-11-21, continued): resuming the real import hit a *third* variant of the same
  partial-import bug — this time Photos' import call itself succeeded for a whole 500-item
  batch, but a later per-item metadata call failed and the batch's own transaction rolled
  back every one of its (otherwise correct) catalogue records with it, orphaning the lot.
  Fixed at the real root: `_import()` now records an item the moment Photos confirms it,
  before attempting its metadata, so a metadata failure can never again undo an import.
  Recovered the resulting 483 orphans (`scripts/recover_orphaned_import.py`, now
  generalised to any batch, not just the Nov 9 one). 7,204 of 150,714 Google items now
  imported. See the session log below.
- Done (2026-11-21/22): resuming hit a *fourth* variant of the same bug class — this time
  Photos' own AppleScript bridge hung completely (no exception, no CPU activity, no window)
  for about 8 hours on `batch-032652`'s `import_assets()` call. 483 items had landed for real
  with nothing recorded. Recovered with the existing script; see the session log below for the
  much larger (473 of 485) unaddressable-metadata count this time, and why `photoscript`'s
  retry wrapper can't rescue a hang like this on its own.
- Next: resume `import --apply` (7 batches left: batch-114078 through batch-147164, plus
  whatever `batch-032652`'s own still-missing 14 pairs add back into the next plan), confirm
  it reaches the end of the plan, then `index --apply` and `report` per the runbook (must
  reach 0 unaccounted). The Dates (90 batches) and Locations queues are optional. Not yet
  looked into: why `duplicate-of-existing` rose from 852 to 1,129 during the Nov 10-21 gap.
  Also not yet looked into: whether the 473 unaddressable items from this session become
  addressable later (a retry pass for their metadata), and whether to make the remaining
  7 batches' run supervised (auto-detect-and-recover a hang) rather than manually watched.
- Coverage: 99.92% line + branch (gate 90%)
- Open questions for Delmar:
  - The rehearsal sample (one shared album) is not on the Air. Rehearse on a slice of the
    real Takeout instead, or skip the rehearsal and go straight to the batched full import?
  - Space: Takeout 445 GB + library after import + one backup copy of the pre-import library.
    Likely fits on 1.8 TB; confirmed once the library size is known.
  - 13 unknown files in the Takeout (two `.3g2` videos, extension-less `IMG_0335`-style files,
    `.smx`): they are flagged, not ingested. Decide whether to support `.3g2`.

## Session log
### 2026-11-21/22: session 6: the retry that also hangs
- Resumed `import --apply` for the 8 remaining batches (3,615 pairs, 28.04 GB) after
  committing session 5's fix. The run's own pre-apply dry run and the confirm prompt went
  fine; the very first batch's `import_assets()` call then hit `photoscript`'s "AppleScript
  timed out: retrying killall + run_script" path once, Photos was killed and relaunched, and
  the *retry* attempt then hung completely for about 8 hours: 0% CPU on both the python
  process and Photos, no Photos window at all (`computer-use` `app_list_windows` → `[]`,
  ruling out a stuck dialog same as session 5), and even a trivial read-only AppleScript probe
  (`count of media items of album`) itself hung rather than erroring.
- Read `photoscript`'s retry wrapper (`script_loader.py`) to understand why: it calls
  `NSAppleScript.executeAppleEvent_error_` directly (an in-process Apple Event dispatch, not a
  subprocess with its own timeout), and only reacts to a timeout *if* that call eventually
  raises one. `tenacity`'s `stop_after_attempt(2)` means exactly one kill-and-retry; if the
  retry's own native Apple Event call never raises (because Photos is still rebuilding itself
  after being killed, on a 1.8 TB / 11,000+ item library on an external volume, say) there is
  no second rescue — it can and did hang indefinitely with no further diagnostic output. This
  project doesn't call `photoscript.configure_run_script()`, so it's on the library's default
  (2 attempts, 5 s between). Not fixed yet (see "Next" above) — this run was recovered by hand
  twice (see below), not by a code or config change.
- Recovery (by hand, not yet automated): stopped the hung background task, force-killed Photos
  (`pkill -9 -x Photos`, since even `tell application "Photos" to quit` needs the same stuck
  AppleScript bridge), relaunched it, and confirmed via `osxphotos` (not AppleScript) that
  the "From Google" album had grown from 6,851 to 7,334 — 483 real, physical imports with
  nothing recorded, the same shape as session 5's incident but caused by a hang instead of an
  exception. Ran `scripts/recover_orphaned_import.py` against this run's own apply plan
  (`import-20261121T152222Z-apply.json`, `batch-032652`): 486 of the batch's 500 pairs were
  already in Photos untracked (the other 14 genuinely hadn't been reached yet, and go back
  into the next `plan()`).
  - The recovery's own `--apply` hung too, the same way, partway through — recovered the same
    way (kill, relaunch, confirm via `osxphotos`, no data lost since each item commits its own
    transaction) and finished: 485 of 486 recorded, 1 failed outright (`photoSetKeywords ...
    User canceled`) and needed one more `--apply` pass to record.
  - New finding, much larger in scale than the Nov 8/21 precedent: 473 of the 485 recorded
    items came back `PhotoNotFoundError` ("Invalid photo id") when `_details()` tried to set
    their metadata — not the ≤5-item exception it was before. All 473 are still correctly
    recorded (favourite/caption/keywords left unset, same fallback as the smaller precedent),
    so nothing will be re-imported or duplicated; only cosmetic metadata is missing for now.
    Plausible explanation, not confirmed: a 500-item chunk imported right before a hard
    `killall` needs much longer than a few stray stragglers to finish whatever background
    processing makes an item addressable via AppleScript (thumbnails/analysis/cloud
    registration) on a library this size, so most of one whole batch was still "not ready"
    when the recovery ran minutes later. Not yet confirmed whether these become addressable
    later; a metadata-retry pass is suggested future work, not urgent (see "Next" above).
  - Caused one more, smaller self-inflicted hang while investigating: an ad-hoc AppleScript
    probe against one of the confirmed-bad uuids (`get favorite of media item id X of album
    Y`, not the same call shape `photoscript.Photo(uuid)` uses) itself hung Photos' Apple
    Event queue for the next ~90 s of unrelated queries too. Recovered the same way
    (kill + relaunch). Lesson: don't probe a known-unaddressable uuid with an untested
    AppleScript shape; stick to `photoscript`'s own addressing method or `osxphotos`.
- Catalogue now 7,843 imports (`batch-032652` at 3,721; up from 3,082 by the 500 recorded
  pairs' rows, still/clip pairs included).
- Resumed `import --apply` again for the real remaining 7 batches. It hung the *same* way a
  third time, now within about 30 minutes rather than hours: 0% CPU on both processes, a safe
  probe timing out twice, catalogue count frozen. Recovered the same way — except this
  recovery round exposed a sharper, separate lesson: after the `pkill -9 -x Photos`, the
  recovery script's own dry run was run *without* relaunching Photos first, and
  `PhotoscriptLibrary.__init__` (`self._script.PhotosLibrary()`) sent an AppleScript event
  unconditionally, even for a dry run that never needed one — which, addressed to an app that
  wasn't running, implicitly tried to auto-launch it and then hung the same way, this time on
  a call nothing in the batch itself could explain. Confirmed via `sample` (stuck inside
  `-[NSAppleScript _executeAppleEvent:...]` → `AESendMessage` → `mach_msg`, i.e. a real,
  pending Apple Event, not a Python-side hang) before concluding this, rather than guessing.
  Fixed procedurally, not in code yet: always explicitly `open -a Photos "<library>"` and wait
  for a responsive probe *before* running the recovery script at all, dry run included, since
  its constructor isn't actually dry-run-safe. (A real fix would defer `PhotoscriptLibrary()`
  construction past the `if not args.apply: return` check — not done this session.) Confirmed
  via `osxphotos` that the hung batch had grown "From Google" from 7,334 to 7,782 (448 of 500
  landed); recovered via the script once Photos was properly relaunched and responsive again,
  which itself hit one more mid-run timeout+kill+retry (handled the same safe way, one
  transaction per item).
- That recovery ran to completion over several hours (checked periodically rather than left
  unattended, given how often this library has needed a kill/relaunch): 447 of 448 recorded
  cleanly, 1 failed the same transient way and needed one more dry-run-then-`--apply` pass
  (itself blocked by yet another Photos hang right after the main run exited — recovered the
  same way: kill, explicit relaunch, confirm responsive, then proceed). 424 of the 447 came
  back unaddressable for metadata (same explanation as before: a large chunk imported right
  before a hard kill isn't done settling yet), consistent with the earlier, smaller sample.
  Catalogue now 8,410 imports (`batch-032652` at 4,288). A fresh dry run next (see "Next"
  above for the figures) to confirm what's genuinely left before resuming `import --apply`.
- Resumed again (6 batches left); hung a *fourth* time, same shape, this time within about
  25 minutes. Recovered the same way: confirmed via `osxphotos` that "From Google" grew from
  7,782 to 8,229 (447 of the new `batch-032652` landed, nothing recorded), killed and
  explicitly relaunched Photos, waited for a responsive probe, then ran the recovery script
  against the fresh apply plan (`import-20261123T064646Z-apply.json`). Its own `--apply` hit
  one more mid-run timeout+kill+retry (same safe per-item-transaction handling) and is still
  running. At this point the pattern is clearly not a one-off: every resumption of this import
  so far has hung within 25 minutes to 8 hours. A proper fix (an external watchdog that
  detects staleness and recovers automatically, or configuring `photoscript`'s retry wrapper
  with more attempts) remains unbuilt this session — manual, periodic checking has been
  sufficient so far, just slow.
- That recovery (batch-032652's 447 orphans, 4th hang) ran cleanly for several hours, checked
  every ~25 minutes, down to 42 pairs left — then hit a *fifth* hang, same shape (one
  timeout+killall+retry logged, then nothing further), but this time with a new and worse
  consequence: the Claude Code session/process itself ended while the retry was stuck, so
  there was nobody to notice or recover it. Photos sat unresponsive for roughly 20 hours
  (confirmed: the process watched all Monday, PID unchanged throughout, was still the exact
  same PID Tuesday afternoon — i.e. `killall Photos` inside `photoscript`'s retry wrapper had
  not actually terminated it despite logging success) until the next check found it. This is
  the clearest demonstration yet that manual periodic checking is not a safety net on its own
  — it only works while a session is alive to do the checking. Recovered the same way:
  `pkill -9 -x Photos` (this time it actually died), confirmed via `osxphotos` that "From
  Google" was unchanged at 8,229 (this hang was mid-metadata-call on one already-recovered
  item, not mid-import, so no new orphans), explicit relaunch, waited for a responsive probe,
  re-ran the recovery dry run (42 of 500 confirmed, matching 447 − 405 already recorded), then
  `--apply` (backgrounded).
- Given this now makes two separate kinds of unattended failure (a hang that outlives a
  25-minute check interval, and a hang that outlives the session itself), an automated
  watchdog — something that keeps checking and recovering even with nobody present — is no
  longer just a nice-to-have; it's been offered to Delmar twice already and not yet built.

### 2026-11-09/21: session 5: the AppleEvent timeout that outlives its own call
- Resumed `import --apply` on Delmar's GO (3 remaining batches, 8,136 items, ~41 GB). Batch
  `batch-032652` (5,195 items) crashed: `photoscript` raised `AppleScriptError` after Photos'
  own AppleEvent timed out waiting for the import call to return.
- Confirmed live (not assumed) that Photos kept importing in the background after our side
  gave up: `osxphotos`/`Album.photos()` showed 1,902 (precisely 1,886 once matched back to
  catalogue items) real assets physically in "From Google" with no `imports` row at all — a
  second, independent flavour of the Nov 8 partial-batch bug, this time from a *crashed* call
  rather than one that returned a `None` for a refused item.
  - Fix: `PhotoscriptLibrary.import_assets` now wraps the `import_photos()` call in
    `try/except AppleScriptError`, falling back to `target.photos()` (the album itself) for
    the by-filename match instead of trusting a crashed call's absent result. 1 new unit test
    (`test_photoscript_recovers_what_photos_actually_imported_before_a_crash`).
  - Prevention: `ImportConfig` gained `batch_items = 500` — batches are now capped by item
    count as well as `batch_gigabytes`, since a single AppleScript call's timeout tracks item
    count (thousands of small files), not bytes. `plan()`'s batching takes whichever limit
    comes first. 1 new unit test (`test_import_batches_also_have_an_item_cap`), 1 new
    integration test (`test_imports_also_go_in_batches_of_the_configured_item_count`).
    RUNBOOK.md updated. 1,268 tests, 99.92% coverage.
  - Recovery (`scripts/recover_orphaned_import.py`, one-off, kept as an audit trail like the
    Nov 8 reconciliation): matches the crashed batch's own saved plan JSON against a live
    `Album.photos()` scan by the `{item_id}-{name}` filename convention, replays
    `ImportStage._working_copy`/`_details` so the backfilled metadata is identical to a normal
    import, and records each via `catalogue.record_import` in one transaction. Dry run first
    (1,886/5,195 recovered, 3,309 "not yet imported" i.e. never reached before the crash, 0
    mismatches against the sample), then `--apply`: "Recorded 1886 items as imported".
    Verified against the live library afterwards.
- This code fix (crash recovery + `batch_items` cap) sat uncommitted for 12 days — `uv run`
  uses the local checkout regardless, so it was already active in every later `import --apply`
  run, but it should have been committed the same session. Committed now (2026-11-21), along
  with this entry.
- Resumed again: completed without crashing, but Photos' own "Cannot Import Item" modal (for
  `32652-baba_13.mov`) silently blocked all further AppleScript calls partway through — the
  same dialog-blocking behaviour as Nov 8, distinguished from the crash above by having no
  timeout/kill evidence in the log. Attempting to dismiss it via background accessibility
  clicks failed silently (reported success, no visible effect); escalating to full-screen
  control then revealed the real block: the screen was locked, which macOS itself disallows
  Accessibility actions on, screen or no screen. Stopped there and asked Delmar to unlock,
  rather than retrying.
- Gap (11 days, not directly observed by this session): by 2026-11-21 the catalogue showed
  6,617 imports, up from 2,309 at the end of session 4 — `batch-032652` grew from the 1,886
  recovered to 2,495 (609 more imported normally once the dialog was cleared and the run
  continued), plus four new, ordinarily-named batches (`batch-073974`/`080482`/`090784`/
  `100627`, 500+500+572+241 = 1,813 items) that are not this session's or session 4's work.
  No sign of another crash or recovery script — these read as plain further `import --apply`
  runs, most likely Delmar resuming by hand after dismissing the dialog, now batching small
  enough (the uncommitted `batch_items` cap) to go through cleanly. `report` confirms this is
  healthy: unaccounted fell from 14,275 to 9,690, nothing else moved unexpectedly except
  `duplicate-of-existing` (852 → 1,129, not yet investigated — plausibly just more Google
  items reaching items already in iCloud via these further batches, not re-examined).
- Committed the Nov 9 fix (crash recovery + `batch_items` cap) and recovery script, then
  resumed `import --apply` for the 9 remaining batches. The confirmation prompt needs stdin
  (`yes | ... --apply`) when run non-interactively; without it the command aborts cleanly
  (nothing changed) rather than hanging.
- Resumed import hit a *third* variant of the same partial-import bug, on the very first
  batch (`batch-032652`'s next 500-item chunk): the bulk `import_assets()` call succeeded for
  the whole batch (confirmed: "From Google" grew from 6,367 to 6,851, ~483 real assets), but
  a later per-item `_details()` call (setting keywords) hit `run_script 'photoSetKeywords'
  failed: User canceled. (-128)` after a timeout-triggered `killall`+relaunch of Photos —
  and because the whole action still ran inside one `catalogue.transaction()`, that one
  item's exception rolled back every `record_import` the batch had already made (there were
  none yet in this case, since `_details` ran before `record_import` for each asset), leaving
  all ~483 imported-for-real assets with zero catalogue record. Confirmed: catalogue
  `imports()` total was unchanged after the failed run (still 6,617); checked for a stuck
  dialog first (none — Photos had no window open at all, ruling out the Nov 8/9 dialog-block
  pattern) before concluding this was a transient AppleScript hiccup, not a lingering block.
  - Root-cause fix (`importer/stage.py`, `_import()`): each asset is now recorded via
    `catalogue.record_import` as soon as `import_assets` confirms its uuid, *before*
    `_details()` is attempted — not after, and not conditioned on it succeeding. A `_details`
    failure (any exception, not just `PhotoNotFoundError`) now only warns
    (`"...could not have their metadata set"`) and moves on to the next asset; it can never
    again undo an already-real import. 1 new `DisposableLibrary` feature (`fails_details`,
    matched by filename like `refuses`) and 1 new integration test
    (`test_what_photos_imports_is_recorded_even_when_its_details_fail`). 1,269 tests, 99.92%
    coverage.
  - Recovery (`scripts/recover_orphaned_import.py`, generalised from the Nov 9 one-off into
    `--plan`/`--batch-target` arguments, now that this is the second incident of this shape):
    its own album scan was switched from `photoscript`'s live AppleScript enumeration to
    `osxphotos` (direct, read-only database reads) after the former failed outright
    ("Can't get media item id ... of album id ...", -1728) on an album this size — a third,
    unrelated AppleScript flakiness, worked around rather than fixed (nothing here needs
    AppleScript for reading, only the actual metadata writes do). Its per-item writes also
    moved from one transaction for the whole run to one per item, so a failure during
    *recovery* itself can't repeat the exact bug it exists to clean up after. Dry run (483
    orphaned of 500, 17 never reached), then `--apply`: 480 recorded cleanly; 3 failed with
    "Invalid photo id" for uuids Photos' scripting dictionary wouldn't address (confirmed via
    `osxphotos`: real, local, non-trashed, non-syndicated JPEGs, just with no `cloud_guid`
    yet — i.e. not yet settled with iCloud) even after quitting and relaunching Photos.
    Matches the Nov 8 syndicated-item precedent closely enough to reuse it: catch
    `PhotoNotFoundError` around `_details()` and record the import anyway (metadata left
    unset) rather than leaving it to duplicate on a future run. Re-ran with that fix: all 3
    recorded. Catalogue now 7,204 imports (480+3 recovered pairs plus their clips); live
    "From Google" unchanged at 6,851 (recovery only adds bookkeeping, nothing new is
    imported).
- Not yet done: resume `import --apply` (8 batches left per a fresh dry run, 3,615 pairs,
  28.04 GB), then `index --apply` and the final `report` (must reach 0 unaccounted).

### 2026-11-08: session 4: the real import, and the AVI container Photos refuses
- Backed up `/Volumes/Photos/Pictures/Photos Library.photoslibrary` (40 GB) to
  `/Volumes/Photos/backup/` with Photos quit, verified (file count, size, opened the copy),
  then `confirm-backup`. `import --apply` ran: batch one (1,606 items) imported cleanly, then
  batch two failed on `32652-baba_13.avi` — "Photos did not import".
- Root cause (confirmed on the catalogue, not guessed): 55 items in the keep set are `.avi`
  files, mostly a Canon camcorder's `MVI_####.AVI` exports (2008-2014). Photos rejects the
  AVI container itself, regardless of the inner codec (live ffmpeg test: H.264-in-AVI muxes
  fine; Photos still refuses it).
- Fix (Delmar: "Convert to MP4 first"): `importer/stage.py`'s `_working_copy` now remuxes an
  `.avi` working copy to `.mov` (`_transcode_avi`, stream copy with `-map_metadata 0`; falls
  back to an `libx264`/`aac` re-encode if the remux fails) before exiftool or Photos ever see
  it. `.avi` was already excluded from `can_write()`, so its metadata was always going to be
  set in Photos after import, not in the file — unchanged by this fix.
  `testing/media.py` and `testing/takeout.py` gained `"avi"` as a real fixture container
  (`TakeoutBuilder.video(..., "clip.avi", ...)`), with the QuickTime-only `-movflags` arg
  guarded off for it; the old "`.avi` is rejected" unit test now uses `.wmv` instead, still
  genuinely unsupported.
- Tests added / coverage: 1 new integration test (avi -> mov, duration confirmed via
  exiftool); 1,260 tests total, 99.92% coverage (gate 90%; the 3 uncovered lines are the
  ffmpeg-missing guard and the re-encode fallback, both error paths not worth faking here).
- Second, independent bug found before resuming: `batch-025960`'s one AppleScript import call
  (5,396 files) had already imported 703 of them into the real library (added to "From
  Google") before hitting the `.avi` file and raising — and raising rolled back the whole
  batch's catalogue bookkeeping, since Photos' own side effect can't be rolled back with it.
  Confirmed live with `osxphotos`: 2,309 assets in the "From Google" album, only 1,606 carry
  the `from-google` keyword that only a recorded import gets. 703 real, live-library assets
  were invisible to the catalogue. Delmar: "fix bug + reconcile".
  - Fix: `PhotosLibrary.import_assets` returns `None` per asset Photos silently left out of a
    batch, instead of raising; `_import` records every asset Photos actually did import
    (always, now) and only warns (`PartialImportWarning`, doesn't stop the run) about the
    rest, which the next run's `plan()` naturally retries as a smaller batch (items without a
    recorded import are never considered done). 1 new unit test
    (`test_photoscript_marks_what_photos_silently_left_out_of_a_batch`) and 1 new integration
    test (partial batch: the untouched items are recorded and detailed, the refused one is
    not, and a fresh `plan()` picks it back up). `DisposableLibrary` gained `refuses=` to
    simulate it. 1,261 tests, 99.92% coverage.
  - Reconciliation of the 703 already-live, untracked assets is separate follow-up work (not
    a code change): matching them to catalogue items by filename via `osxphotos`, back-filling
    `imports`, and finishing their keywords/dates/albums the same way a normal import does.
  - Real root cause, found while sizing the reconciliation: it was never really about `.avi`
    (only 9 of batch-025960's 5,396 files are `.avi`). `batch-000186` (1,606 items, ~18.6 GB)
    imported in full; `batch-025960` (5,396 items, also ~18.6 GB) stopped dead after item 703
    — a clean cutoff, not scattered failures. Photos' one AppleScript call per batch has some
    practical limit that tracks item count (or the AppleScript call's own overhead), not
    bytes. `.avi` genuinely needed fixing regardless, but it was not what actually stopped
    this batch.
  - Reconciliation run (`/tmp` scratch script, not committed — a one-off recovery, same
    pattern as the earlier survey scripts): dry run first (703/703, 0 failures, confirmed
    every item's metadata was already fully baked into its working copy, nothing left
    "unwritten" for Photos to set), then for real — `_working_copy` + `_details` +
    `record_import` per item, each in its own catalogue transaction. 703/703 recorded, 0
    failures. Verified against the live library with `osxphotos`: 2,309 imports recorded =
    2,309 in "From Google" = 2,309 carrying `from-google` (0 orphans left); a keyword sample
    showed real `People/…` tags, confirming `_details` ran for real, not just the album add.
    Catalogue backed up first to
    `/Volumes/Photos/photomigrate/catalogue-before-orphan-reconcile-20261108T151153Z.sqlite`.
- Resumed with `batch_gigabytes = 2.0` (scratch config, not a default change) — 21 smaller
  batches instead of 4. The first 8 all failed 100%: the original crashed run's own "Cannot
  Import 5,084 Items" dialog was still open and unacknowledged in Photos, and a modal sheet
  like that blocks every further AppleScript call against the app, not just the one that
  raised it. Background clicks (`app_click`, AX press on the sheet's OK button) reported
  success but never actually changed anything on screen, confirmed by screenshot three times
  running; only full-screen control (Delmar approved it) with real clicks dismissed the ~9
  stacked dialogs (the original plus one per failed batch, each matched to its batch by its
  exact reported item count).
- With the dialogs cleared, the run hit a second, genuinely separate bug: fatal error on
  `114487-PeppaCapture11.png`, `exiftool could not write ... non-zero exit status: 1`. The
  file's bytes are JPEG, not PNG (confirmed with `file`) — a Picasa-era mislabelling in the
  Takeout (`software=Picasa` in its real Exif header) — and `exiftool` rightly refuses to
  write metadata into a content/extension mismatch. `_working_copy` let that `MetadataError`
  propagate uncaught, which aborted the *entire* run via `StageFailedError`, not just that one
  item.
  - Fix: catch `MetadataError` around the `exiftool.write()` call in `_working_copy`; on it,
    keep the working copy's original bytes untouched (`shutil.copyfile`, same fallback path
    already used for formats `can_write()` excludes) and return its metadata as "unwritten",
    so Photos sets it directly after import instead. Warns (`PartialImportWarning`, reused)
    rather than stopping the batch. 1 new integration test
    (`test_a_file_whose_bytes_dont_match_its_extension_imports_with_its_metadata_unwritten`).
    1,262 tests, 99.92% coverage.
  - First attempt at the fix defensively `.unlink()`ed the destination before the fallback
    copy — caught immediately by the project's own `no-delete-check`
    (`tests/unit/test_no_delete_check.py`), which has no exemption for a genuine delete, only
    for moves/renames. Fixed by dropping the `.unlink()` entirely: `shutil.copyfile` already
    overwrites whatever is at `dest`, so it was both forbidden and unnecessary.
- Resumed a third time: the 13 remaining `import-batch` actions (including the retry of
  `batch-111459`, this time past the PNG without crashing — Photos itself refused
  `PeppaCapture11.png` along with 334 others, "Unknown error") all succeeded; the "Cannot
  Import 335 Items" dialog that followed had already been dismissed by the time full-screen
  control was approved, so nothing needed clicking through this round.
- A third, genuinely different bug then stopped the run on the first `stage-to-delete` action:
  `photoscript.Photo('19EDFF50-…')` raised `ValueError: Invalid photo id`, even though
  `osxphotos.PhotosDB(...).get_photo(...)` finds the same uuid fine (`intrash: false`,
  `hidden: false`). Its `path` gave it away: `.../scopes/syndication/originals/1/....jpeg` — a
  syndicated Shared-with-You item from Messages, which gets a database row but isn't a real,
  scriptable Photos asset (confirmed only 5 such items exist in the whole library).
  - Fix: `library.py` gained `PhotoNotFoundError`, raised by a new `PhotoscriptLibrary._photo()`
    helper (now the single place every `photoscript.Photo(uuid)` call goes through) when
    AppleScript doesn't recognise the uuid. `ImportStage.apply()` catches it around
    `stage-to-delete`/`unstage-to-delete`, `set-date` and `set-location` — the only action
    kinds that address an *existing* iCloud item by uuid — and warns (`PartialImportWarning`,
    reused) instead of raising, so one unreachable existing item doesn't stop the whole stage.
  - Unlike the import-side fixes, this one does *not* get retried by a later run: these three
    action kinds use the runner's generic "already applied" tracking (by action key, in the
    run log), which only knows success/failure, not "skipped" — adding a real third status
    would need a schema migration on the live catalogue, disproportionate for ≤5 items that
    could never succeed anyway (Photos will never recognise them). The warning at the time is
    the only record; `DisposableLibrary` gained a matching `unaddressable=` for tests.
  - First draft of the test asserted a later `plan()` would retry the skipped item — wrong, for
    the reason above; the generic run-log tracking (not the stage's own `catalogue.staged()`
    check) is what actually suppresses it. Fixed the test to assert no retry once that was
    understood from a failing run, not guessed. 2 new integration tests, 1 new unit test
    (`PhotoscriptLibrary` raising `PhotoNotFoundError`). 1,265 tests, 99.92% coverage.
- Not yet done: resuming the real `import --apply` for what is left (all three bugs fixed),
  then `index --apply` and the rehearsal `report` (must say 0 unaccounted).

### 2026-10-29: session 3 (continued): the review, recorded
- Gallery fixes from Delmar's first look: a "Remove these" answer on Dates (nothing in the
  batch is imported); map tiles served through the gallery with a User-Agent and a disk cache
  (OpenStreetMap blocked the browser's requests); the Dates queue leaves out what other
  queues drop (163 batches to 90).
- Delmar: clicking through 100k cards is impossible; propose the keep set year by year
  instead. The proposal page (local, family photos, not published) showed every year's counts
  and samples; Delmar approved it.
- New `photomigrate accept` stage (dry run unless `--apply`): records each unanswered proposal,
  with `--edit-only/--keep-pairs` and `--drop-flagged/--keep-flagged`. 100,902 answers.
- Bug found by comparing the recorded keep set with the projection: a copy of a keeper whose
  edit was kept in its place took "original version" as its reason, so the copy's own edit
  was imported too. A keeper that only gave way to its edit now still counts as kept; its
  copies are duplicates and their edits go with them. That removed 1,113 more edits (567
  byte-identical to a kept edit, 540 edits of low-resolution copies).

### 2026-10-27/28: session 3: the real Takeout on the Air
- Takeout: 46 parts in three archives (`-1-001..022`, `-2-001..023`, `-3-001`), 444 GB,
  downloaded in Chrome straight to `/Volumes/Photos/takeout` (copied cookies stopped working
  within a minute: Chrome binds the Google session to the browser, so
  `scripts/download-takeout.sh` could not be used). Every part passed `unzip -t`; a `.sha256`
  per part, in the script's format.
- Drive: reformatted from case-sensitive to plain APFS (Photos libraries need it), renamed
  `Photos`, ownership enabled, owned by Delmar.
- Fix: several archives. Google numbers clashing names within an archive, not across them:
  4,625 media paths are in two or three archives with different contents. Entries now have an
  `archive` and a `key` (`Takeout-<n>/...` outside archive 1); sidecars match per archive,
  albums per folder. `Takeout.entry()` is a dict lookup (was a scan per item).
- Fix: macOS 27 Vision raises "key does not exist" for an empty options dict; pass None. The
  15 macOS tests that failed on the Air pass.
- Ingest dry run: 153,302 items (136,064 photos, 17,238 videos, 477 GB), 58 albums; matches:
  99,003 exact, 44,085 edited-original, 10,214 without a sidecar; 648 orphan sidecars;
  13 unknown files.
- Fix: ingest was killed (exit 137) after 5,991 items at an 11 GB footprint. On macOS 27 a
  growing `BytesIO` never returns its memory, and the Vision calls had no autorelease pool.
  `vision_data` sizes the buffer up front; face and text reads drain a pool. Ingest then held
  ~3.2 GB for the whole run (a macOS test pins it).
- Ingest apply: 11 h on one core (CPU-bound; the USB drive ran at 7-14 MB/s of ~400).
  150,714 items: 136,052 photos (103.5 GB), 14,662 videos (364.9 GB); 2,588 byte-identical
  copies folded into their items; 7,320 Live Photos paired; 132 unpaired motion clips,
  648 orphan sidecars and 13 unknown files flagged; 0 failed.
- Fix: the date audit's suggestions scanned the whole camera sequence for each flagged shot
  (quadratic; 20,000 shots took over 2 minutes). Bisection now; the test runs in under a second.
- Analyse apply (Google only, proposals for the review; 37 minutes):
  - duplicates: 23,479 groups; 27,135 extra photos (24.7 GB) and 1,886 extra videos (94.3 GB);
  - best shot (after duplicates): 7,848 groups, keeping 15,696, setting aside 9,026 (7.8 GB);
  - edits: 44,084 edited/original pairs, 112 edits without an original;
  - quality flags on 8,255 items (3,766 blurry, 2,725 low-resolution, 1,958 badly exposed,
    115 accidental, 222 bad video clips);
  - clutter: 5,009 (2,526 Google creations, 1,248 documents, 932 screenshots, 303 memes);
  - 3,730 proposed locations; 171 date batches with doubtful or missing dates;
  - in all 43,040 items (132.5 GB) proposed to set aside, of 150,714 (468 GB).
- Branch `fix/takeout-multiple-archives` pushed (8 commits, 1,222 tests, 100%).
- Delmar moved the Photos library to the drive (`/Volumes/Photos/Pictures/Photos
  Library.photoslibrary`, the whole Pictures folder came along; the paths are arguments, so
  it stays there), made it the System Photo Library, downloaded originals (38 GB) and gave
  the Claude app Full Disk Access. UTM VM shut down; Spotlight off for the drive.
- iCloud is small: 3,754 items (3,291 photos, 463 videos, 35 GB), nearly all from 2021 on.
  Google Photos was Delmar's main store before that, so Google holds the history.
- Fix: a read-only `sqlite3` query on the catalogue while `index --apply` ran made a write
  wait past SQLite's timeout ("database is locked"). The catalogue now uses write-ahead
  logging. Note: with WAL, `sqlite3 -readonly` cannot open the catalogue when no stage has
  it open; use plain `sqlite3` for queries.
- Index apply: 3,754 iCloud items, resumed after the lock.
- Delmar's decision (2026-10-28): over 100k is still too many; keep the best 20% of each
  year's photos. Always keep favourites (they count towards the 20%);
  videos are left out of the cut. iCloud photos are cut like Google's (the ones left out go
  to "To Delete"). Album photos were protected at first, but one album ("Milan en Jacques")
  holds 29,068 photos, which kept 33,663 safe; Delmar then chose no album protection.
- New stage `highlights` (CLA follow-up): per year, events split at 6-hour gaps, the 20%
  quota shared across events by size, ranked by the best-shot scores blended with an
  open_clip "good photo" appeal (0.4), passing over near repeats (0.9). One `highlights`
  group per event in a new review queue (the best-shot picks form; it may keep none).
  Outcomes: edits follow their original; copies of a keeper the review turns down go with
  it (that gap existed for best shot too). Catalogue v17. `[highlights]` in the config.
- Fix: a Google edit only followed its original's clutter or highlights answer, so the edits
  of best-shot losers and duplicates were still imported (~16,000). An edit now goes where its
  original goes, unless the review kept only the edit.
- Analyse on both sources, every proposal accepted (simulated on an in-memory copy): 15,851
  photos and 6,153 videos kept (from 140,000+ photos and 15,000 videos). Busiest years: 2014
  2,049 photos, 2011 1,872, 2012 1,692, 2013 1,546, 2015 1,437. Photos with a Google edit
  keep both versions (edit policy "pair").
- Delmar: cut videos too. Same 20% per year, in groups apart from photos, ranked on
  resolution, length, exposure and motion (videos have no embedding). Live Photo clips follow
  their still.
- With every proposal accepted: 15,851 photos (12.8 GB) and 1,776 videos (63.3 GB) kept, from
  468 GB of Google items and 35 GB of iCloud. Next: the review gallery.

### 2026-10-21: session 2 (continued): the real Photos.app test
- Both opt-in Photos.app tests pass on the Air with a throwaway library in /tmp: the fixture
  import (albums, favourite, caption, keywords), and a genuine iPhone Live Photo, which Photos
  joins into one asset.
- Fixed on the way:
  - the test guard allows /tmp;
  - an unreadable-settings error now names the missing Full Disk Access;
  - an unjoined Live Photo clip is recorded as its own asset.
- The Air: Terminal and sshd-keygen-wrapper have Full Disk Access; tmux is installed.
  Photos-driving commands run in a tmux session started in Terminal at the Air (macOS never
  offers Automation to ssh sessions). RUNBOOK says so.

### 2026-10-21: session 2: Delmar's answers and the runbook review
- Delmar's answers:
  - best-shot stays at 0.94 until he judges the groups in the rehearsal;
  - location backfill borrows across devices (`any_device = true`);
  - he is getting the drive;
  - a one-off copy of the library is the backup (no Time Machine);
  - permissions are taken back after the migration;
  - one catalogue for the rehearsal and the full run, all on the drive;
  - two extra checks in Photos: the asset count, and the iPhone sync.
- Code:
  - `confirm-backup` asks for a full copy of the library;
  - `Catalogue.own_imports()`: a re-index after an import keeps the importer's own assets
    out of every analysis stage and out of To Delete;
  - `report` gives the imported asset count, to compare with "From Google" in Photos.
- Runbook rewritten accordingly (`PHOTOMIGRATE_*` variables, `$W`, `"$L"`).
- Tests: 1,163, 100%.

### 2026-10-21: session 1 (continued): M4-04 (runbook)
- docs/RUNBOOK.md: preparation, backup, permissions, rehearsal on the sample (its own
  catalogue), Delmar's checks in Photos, the full run in batches, the final report, and the
  manual cleanup. There is a GO checkpoint before every `--apply`.
- Dry rehearsal of the commands on the sample here (no library): it showed that `$R`
  expansion fails in zsh (the runbook now uses the `PHOTOMIGRATE_*` variables), and that
  best-shot pairs counted as pending although nobody is asked about them (fixed, tested).
- Tests: 1,159, 100%.

### 2026-10-21: session 1 (continued): M4-03
- `photomigrate report`: HTML and CSV, one status per Google item (imported with uuid,
  duplicate-of-existing with the uuid that stays, rejected-by-user with the reason, or
  unaccounted with why), totals and bytes (catalogue v16 `items.size`), the SHA-256 trail,
  date and location fixes, To Delete count; exit 1 while anything is unaccounted.
- Fix found by the report: edits of Google creations follow their original's clutter
  answer; unpaired edits are queued themselves.
- Tests: 1,158, 100%. The full-pipeline test ends with a clean report.

### 2026-10-21: session 1 (continued): M4-02
- `import` stages and unstages "To Delete" so the album is exactly the rejected iCloud items
  (catalogue v15 `staged`); `PhotosLibrary.remove_from_album` (album membership only).
- `safety.NoDeleteGuard`, used by `import --apply` on the library (the Takeout stays under
  `ReadOnlyGuard`). The static check now catches `getattr` lookups, Photos delete scripts,
  and moves/renames (allowed only with a reason comment).
- A full-pipeline test with every delete API rigged to fail on media.
- Tests: 1,138, 100%.

### 2026-10-20: session 1 (continued): M4-01
- `importer.outcomes`: every Google item is import, duplicate-of-existing, rejected-by-user,
  follows (Live clip) or pending (an unanswered question); `to_delete` lists the rejected
  iCloud items.
- `importer.library`: `PhotosLibrary` protocol; `PhotoscriptLibrary` refuses unless Photos
  has the given library open. `testing.photos.DisposableLibrary` for tests.
- `importer.stage` + `photomigrate import` and `confirm-backup`: batches of working copies
  (exiftool `-o`) into "From Google", with albums, favourites, captions and keywords; dates
  and places for existing iCloud items; catalogue v14 `imports`. Config `[import]
  batch_gigabytes`; the config file can now name a section differently from its field.
- Tests: 1,109, 100%. The real Photos.app test is opt-in (see the open questions).

### 2026-10-20: session 1 (continued): M3-06
- `photomigrate review` (FastAPI, Jinja2, htmx 2.0.4 vendored and checked against its
  published SHA-384): eight queues; decisions are checked, then saved at once; bulk answers
  for clutter categories and edit-pair policies; cached thumbnails from the zips and the
  library (HEIC, video posters); keyboard shortcuts; progress bars.
- Bound to 127.0.0.1 only (tested on a real socket, and seen live with lsof). Other host
  names are refused, and writes need a same-origin Origin header.
- Real sample (a /tmp copy): pages render in 10 to 90 ms; thumbnails take 68 ms from the
  4 GB zip.
- Tests: 1,062, 100%.
- Handoff for M4-01: decisions to read are listed in DESIGN, "Review gallery". Also
  `date_fixes.planned_write` (dates), `locations.approved_location` (GPS plus the keyword
  `location-inferred`), and the clutter, quality and unmatched rejects.

### 2026-10-20: session 1 (continued): M3-05
- `photomigrate.timeline` parses the iOS Timeline export (visits, activities, paths) and
  locates a moment; `testing.timeline.TimelineBuilder` writes synthetic exports.
- `analyse.locations` proposes places for shots without GPS from the same camera's located
  shots within 30 minutes (interpolated between two), else from `--timeline`. Proposals
  (catalogue v13 `inferred_locations`) are written only once approved (`approved_location`,
  keyword `location-inferred`). `any_device` switch; config accepts `true`/`false`.
- Real sample: nothing to propose with the same device (iPhone 4S shots have no GPS at all);
  56 across devices. Question posted on CLA-87.
- Tests: 1,000, 100%.

### 2026-10-20: session 1 (continued): M3-04
- Ingest and index detect text with Vision (`text_regions`, catalogue v12 `items.text`); index
  records Photos' `screenshot` flag. open_clip text prompts (`embed_texts`) give zero-shot
  labels where Vision measured no text.
- `analyse.clutter` scores google-creation, screenshot, document, meme and forward per item
  (evidence combined as independent clues, camera penalties), and queues the best category
  at or above `[clutter] min_score` (0.5) in the `clutter` table with its evidence.
- Real sample: the first rules queued 86 "documents" from Vision boxing textured scenes and 32
  "screenshots" that were mostly 1080p videos. After the fixes: about 138 creations,
  7 documents, 7 screenshots.
- Tests: 956, 100%.
- Handoff: M3-06 shows the queue by category with the evidence and records decisions per
  item; Google creations are never imported unless Delmar picks them there.

### 2026-10-20: session 1 (continued): M3-03
- Delmar asked to keep going through every milestone that needs no human action without
  waiting for confirmation (the CLAUDE.md safety rules still apply).
- Catalogue v11 `items.camera` (EXIF/QuickTime make and model) and a decisions API
  (`set_decision`, `decision`). File-name dates: macOS screenshots (12-hour too), Telegram,
  Android, bare dates; Hypothesis round trips.
- `analyse.date_audit` flags missing, placeholder, future, upload-as-taken, conflicting
  (> 1 day) and out-of-sequence dates, and batches them by problem, camera, folder and prefix
  with per-item suggestions and a batch offset (`date-batch` groups).
- `analyse.date_fixes` applies one answer per batch (accept, keep, date, year → 1 July noon
  + `date-approx`, exact shift): `fix-date` / `plan-timewarp` actions store a `user` date.
  `planned_write` gives the importer ingest's plan with that date on top.
- Real sample (/tmp only): 1 batch of 3 (DESIGN, "Date audit").
- Tests: 888, 100%.
- Handoff: M3-06 must show date batches (reasons, current dates, suggestions, offset) and
  store the answer with `set_decision("group", id, "date", {...})`; M4-01 writes Google
  working copies from `planned_write` and runs timewarp for iCloud items with a `user` date.

### 2026-10-20: session 1 (continued): M3-02
- Ingest and index now embed every photo with open_clip (ViT-B-32, LAION-2B, MPS) and read
  faces with Apple Vision (capture quality) plus Core Image (eyes closed), from the same
  decode. Catalogue v10: `items.embedding` (float16) and `items.faces` (JSON, NULL = not
  measured). torch comes from the CPU wheel index on Linux.
- `analyse.best_shot`: groups within a 2-minute window by embedding similarity, ranks on
  sharpness, exposure, faces, eyes open and Apple's aesthetic score (weights in
  `[best_shot.weights]`, only scores every shot has), keeps the top two, proposes the rest
  for "To Delete" (`best-shot` groups). Without Vision, faces are left out.
- Tests never download the model: `ClipEmbedder` runs with random weights, grouping tests use
  `testing.embedder.ThumbnailEmbedder`, and an autouse fixture gives the CLI that stand-in.
  Vision is tested on the osxphotos test library.
- Real sample (/tmp only): ingest 4 min 43 s with CLIP and Vision. 216 groups, 351 shots
  proposed at the 0.94 default (661 at 0.90). See DESIGN for the measurements.
- Tests: 796, 100%.
- Handoff: the review gallery (M3-06) should show each best-shot group ranked, with the
  score parts from the group details, and let Delmar confirm or swap the kept two.

### 2026-10-20: session 1 (continued): M3-01
- `photomigrate.quality` measures photos (sharpness, exposure, contrast, size) and videos
  (brightness, motion) at ingest and index: one decode per photo for hashes and metrics, one
  ffmpeg pass per video for signature and metrics. Catalogue v9: `items.quality` and
  `quality_flags`.
- `analyse.quality` flags items with scores from 0 to 1; thresholds come from the new
  `--config` TOML file (`photomigrate.example.toml`).
- Runner fix: an action undone by a later run is applied again (a setting changed, then
  changed back, used to leave the middle setting in place).
- Real sample: 3,308 items measured. 29 photos flagged `blurry` (0.9%), nothing else. Dedupe
  results are unchanged by the single decode.
- Tests: 720, 100%.

### 2026-10-20: session 1 (continued): dedupe precision from a pixel check
- Delmar asked for a real image comparison (and not to trust filenames). Every proposed pair
  on the sample was decoded and compared by pixels.
  - The first rules gave 17 false positives out of 287.
  - The fixes: exact EXIF second; star groups around the keeper; a stricter 256-bit threshold
    when a time is missing.
  - Now 276 of 276 are confirmed the same image. Recall: 2 borderline possible misses out of
    2,258 similar ungrouped pairs.
- Tests: 665, 100%.

### 2026-10-20: session 1 (continued): M2-04 and a real-sample analysis
- `analyse.edited` (`edit-pair` groups, `--edited-policy`); catalogue v8 (`groups` rebuilt;
  migrations run with foreign keys off, then verified).
- Real sample (/tmp only):
  - ingest of the whole album took 80 s with all the hashing;
  - 1,562 edit pairs, no orphans;
  - 256 photo duplicate groups, mostly `IMG_1234.JPG` + `IMG_1234_2.jpg` re-uploads, which are
    true duplicates;
  - no video duplicates.
- The review gallery (M3-06) must show edit pairs side by side, with a bulk policy switch
  (commented on CLA-88).
- Tests: 662, 100%.

### 2026-10-20: session 1 (continued): M2-03
- Video signatures (`hashing.video_signature`, 8 sampled frames) and bitrate at ingest and index
  (catalogue v7). `analyse.dedupe_video` plus `analyse.grouping` (shared by both dedupe stages,
  which scope their groups by key prefix). `photomigrate analyse` runs both.
- Tests: 649, 100%.

### 2026-10-20: session 1 (continued): M2-02
- `analyse.dedupe_photo` plus `photomigrate analyse`; a 256-bit pHash (`phash_fine`, catalogue v6);
  Google photos are now hashed at ingest; `synthetic_image(jitter=…)` gives "a moment later" shots.
- The thresholds came from measurements (see DESIGN). The same-moment tolerance is 1 s, because
  copies keep identical EXIF times.
- Tests: 619, 100%.

### 2026-10-20: session 1 (continued): M2-01
- `index.icloud` plus `photomigrate index`; `hashing` (SHA-256, pHash/dHash); `safety.ReadOnlyGuard`
  and `announce_write`; catalogue v5 (scores, timezone).
- Finding: osxphotos' `mode=ro` SQLite reads still bump the library's `-shm` mtime. That
  led to the shadow-library design, so the real library is now untouched, mtimes included.
- Tests: 569, 100%. The macOS tests use osxphotos' test libraries (fetched into tmp,
  about 200 MB, pinned to v0.77.2).

### 2026-10-20: session 1 (continued): M1-06
- Live Photo pairing (`ingest.livephotos`, the `live-photos` stage run by `ingest`).
  Catalogue v4 adds `content_id`, the groups API and `add_flags`. `dates.moment_gap` and
  `same_moment` are shared with the date policy.
- Tests: 508, 100%. The pairing property test passed at 5,000 examples.
- Deferred to M2-02: marking a Google pair as a duplicate when iCloud already has that Live Photo.

### 2026-10-20: session 1 (continued): real-sample checks and M1-05
- Real-sample smoke test (approved by Delmar). It ran on this work Mac from `data-in/` into
  /tmp only; the zips were unchanged.
  - Matching: 3,307 of 3,308 matched. The one leftover pair is genuine: a video without a
    sidecar, and a sidecar whose video isn't in the export.
  - Apply took 21 s. There are 21 date conflicts and 171 sidecar-date fills, and no EXIF offsets
    at all (posted on CLA-85).
  - With M1-05: 1 shared album with 3,308 memberships and 16 distinct `People/` keywords.
- The Air is set up (`~/Code/photo-library-migration`, exiftool 13.55, ffmpeg 9, uv) and all
  tests pass there.
- M1-05: catalogue v3 adds albums, members, item_copies, keywords and google_url. Ingest
  resolves album copies into one item, and adds memberships, favourites, captions and People
  keywords.
- Tests: 472, 100%.

### 2026-10-20: session 1 (continued): M1-04 and a new plan
- Facts that changed the plan:
  - The Google export is **~600 GB** (12 × 50 GB parts), not 30–40 GB.
  - The run host is Delmar's home **MacBook Air** (`ssh openclaw`; macOS 27, 16 GB, about
    40 GB free internally) with an external APFS drive.
  - EC2/S3 was dropped: the only AWS profile is an Amazon Isengard account, and personal photos
    don't belong there.
  - Delmar keeps the existing export.
- What changed:
  - Ingest reads straight from the zips (`ingest.takeout`, `names`, `sidecars`, `exif`,
    `policy`, `stage`). The CLI has `ingest` plus the global `--catalogue` / `--plan-dir`.
    Catalogue v2 adds `metadata_plan`.
  - The safety guard forbids `data-in/` and judges relative names by the cwd only.
  - The Timeline export (`data-in/Timeline.json`, iOS format, 2025–2026 only) is surveyed;
    see the comment on CLA-87.
- Tests added / coverage: 448 tests, 100%. The Hypothesis property tests also passed at
  5,000 examples (`HYPOTHESIS_PROFILE=stress`).
- Decisions made:
  - Working copies are written at import (M4-01), not at ingest. Catalogue paths are relative
    (`<part>::<member>`).
  - A sidecar date goes into the home time zone with an `offset-assumed` flag.
    GPS-based time zones are a possible later improvement.
  - Altitude is written only together with latitude and longitude.
- Handoff notes for next session:
  - M1-05 adds album memberships, favourites, captions and people keywords; the people names
    are already in each ingest action's `sidecar_data`.
  - The real-sample smoke test is a dry run of `photomigrate ingest data-in` with
    `--catalogue` and `--plan-dir` under /tmp. Ask Delmar before running it.

### 2026-10-20: session 1 (continued): M1-03
- Issues worked: CLA-75 [M1-03].
- What changed: `photomigrate.catalogue` (schema v1, migrations, idempotent item and
  date-candidate upserts, nested transactions, in-memory snapshots) and `photomigrate.runner`
  (Action/Plan/Stage, dry-run vs apply, resume, plan JSON and rich tables). The guard now
  decodes percent-encoded sqlite `file:` URIs.
- Tests added / coverage: 262 tests, 100%.
- Decisions made:
  - Dry runs never open the catalogue for writing. They plan on an in-memory copy.
  - Re-running a finished stage writes nothing at all, not even a `stage_runs` row.
  - Group, decision and membership APIs are schema-only until the issues that use them
    (M1-05, M2, M3) add methods with their own tests.
- Handoff notes for next session:
  - M1-04 wires the CLI: global `--catalogue` / `--plan-dir` options and `ingest --apply`,
    using `run_stage` + `render_result`. Defaults are `runner.DEFAULT_CATALOGUE` and
    `DEFAULT_PLAN_DIR`.
  - Stage `apply` must be idempotent on disk: write only when the content differs.

### 2026-10-20: session 1 (continued): M1-02
- Issues worked: CLA-74 [M1-02].
- What changed:
  - `photomigrate.testing.media`: seeded synthetic images (JPEG, HEIC, PNG with EXIF date,
    offset, camera and GPS), animated GIFs, and tiny bit-exact H.264 MP4/MOV via ffmpeg.
  - `photomigrate.testing.takeout`: the naming rules, sidecar and album JSON, a
    `TakeoutBuilder` with a ground-truth `TakeoutManifest`, deterministic multi-part zips, and
    `standard_takeout()` with every quirk. Session fixture: `standard_takeout_zips`.
  - tests/fixtures/README.md (quirk catalogue). CI linux now installs ffmpeg and exiftool.
- Tests added / coverage: 219 tests in total, 100%.
- Decisions made:
  - The generator lives in `src/photomigrate/testing/`, not `tests/fixtures/` as in the
    playbook layout, so mypy --strict and the coverage gate apply to it.
  - Pillow and pillow-heif are runtime dependencies (the tech stack needs them later anyway).
  - Name truncation is a documented model (docs/TAKEOUT_FORMAT.md, "Modelled rules"); the
    real sample has no long names to confirm it.
- Handoff notes for next session:
  - Ingest tests should assert against `TakeoutManifest` (`metadata_sidecar()`, `parts`,
    relations), not re-derive the naming rules.
  - Calling `edited()` on a 47-character name makes the same name as the original, which
    raises "already in this Takeout". Real behaviour is unknown, so it is not modelled.

### 2026-10-20: session 1
- Issues worked: CLA-73 [M1-01].
- What changed:
  - Public repo `delmarolivier/photo-library-migration`; squash-merge only, branches deleted
    on merge, `main` protected (required checks, no direct pushes, admins included).
  - uv project (Python 3.12, src layout), Typer CLI with `--version` and stub stages
    (ingest, index, analyse, review, import, report).
  - CLAUDE.md, .claude/settings.json (verbatim from the playbook), .gitignore for media, data,
    Takeout, catalogue DBs and secrets.
  - pytest (branch coverage, 90% gate, `macos` marker, xdist, hypothesis profiles), ruff,
    mypy --strict, pre-commit.
  - `photomigrate.safety`: audit-hook media guard + pytest plugin, `announce_path`, and the
    AST no-delete check (`photomigrate-no-delete-check`).
  - GitHub Actions: `linux`, `no-delete-check`, `macos` (PRs only), combined `coverage` gate.
- Tests added / coverage: 113 tests, 100%.
- Decisions made:
  - Repo is **public**, not private as the playbook says (Delmar, 2026-10-20).
  - macOS CI job runs **on PRs only** (Delmar, 2026-10-20). The combined coverage gate needs
    it, so it is PR-only too; pushes to `main` run `linux` and `no-delete-check`.
  - Commits use the GitHub noreply email (repo-local git config), so no work email appears in
    public history.
  - The guard is an audit hook rather than a monkeypatch, so it also catches subprocesses
    (exiftool, ffmpeg) and sqlite3 (osxphotos). See docs/DESIGN.md.
  - Google-made creations go to the clutter review queue; Takeout people names become
    `People/<name>` keywords (Delmar, 2026-10-20; added to CLAUDE.md product decisions).
- Blockers / questions: see "Open questions" above.
- Handoff notes for next session:
  - Fixture media must be generated under `tmp_path`, or committed under `tests/fixtures/`
    (an allowed root). Media extensions are gitignored, so committed samples need a
    `.gitignore` negation for `tests/fixtures/`.
  - Any exiftool wrapper using stay-open mode must call `announce_path()` per file.
  - `data-in/` holds real Takeout samples (Delmar, 2026-10-20). Agreed use: a structural
    reference for M1-02 (see docs/TAKEOUT_FORMAT.md), then an opt-in, local-only, dry-run
    ingest smoke test after M1-04. It never runs in CI, and Delmar confirms before its first run.
    The survey scripts live outside the repo (/tmp/photomigrate-survey) and print shapes only.
