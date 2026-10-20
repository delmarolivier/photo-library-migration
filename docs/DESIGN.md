# Design notes

Takeout structure and quirks: see [TAKEOUT_FORMAT.md](TAKEOUT_FORMAT.md).

## "To Delete" and the no-delete guarantee (M4-02)

- **Staging** (part of `photomigrate import`): `stage-to-delete` puts every existing iCloud
  item the review rejected (`outcomes.to_delete`) in the album "To Delete". That covers a
  confirmed duplicate of another item, a best-shot loser, and clutter, quality or unmatched
  rejects. Delmar deletes them by hand.
  - `unstage-to-delete` takes an item out when its rejection is withdrawn, so the album is
    always exactly the rejections. Taking an asset out of an album never deletes it.
  - Catalogue v15 `staged` remembers what is in the album and why.
  - Rejected Google items are simply not imported.
- **At runtime:**
  - `safety.NoDeleteGuard(roots)` is an audit-hook guard. It refuses removing (`os.remove`,
    `os.rmdir`, `shutil.rmtree`), renaming and moving (`os.rename`/`os.replace`,
    `shutil.move`) anything inside its roots; adding files is fine.
  - `import --apply` runs under `ReadOnlyGuard(takeout)` and `NoDeleteGuard([library])`.
  - `tests/integration/test_no_delete_pipeline.py` runs the whole pipeline (ingest, analyse,
    a review, confirm-backup, import, staging) on the standard Takeout with every delete API
    rigged: `os.remove`, `os.unlink`, `os.rmdir`, `os.removedirs`, `shutil.rmtree`,
    `Path.unlink` and `Path.rmdir` raise on the Takeout or the library, and Photos' delete
    commands always raise. A `NoDeleteGuard` watches both folders. It checks nothing was
    refused, the zips are byte-identical, and the rejected iCloud item is in "To Delete",
    not gone.
- **Statically** (`photomigrate-no-delete-check`, CI): it now also catches
  `getattr(x, "remove")` and other denied names looked up by string, and AppleScript or JXA
  snippets for Photos that `delete`. The "To Delete" album name doesn't count; the command is
  lowercase. It also catches moves and renames (`os.rename`, `os.replace`, `shutil.move`,
  `.rename(`) unless the line says `# no-delete-check: allow (why)`. Only the Takeout
  extractor's move of its own staged copy carries that. Deletes can never be allowed.

## Import (M4-01)

`photomigrate import --takeout DIR [--library PATH --apply]` (`importer.stage`). It's a dry
run unless `--apply`. `--apply` also needs the backup marker (`photomigrate confirm-backup`
asks whether a Time Machine backup of the library finished, then writes
`data/BACKUP-CONFIRMED`) and a yes at the prompt.

- **What each item becomes** (`importer.outcomes`, shared with To Delete and the report):
  - A Google item is `import`, `duplicate-of-existing` (a confirmed duplicate, with the copy
    that stays), `rejected-by-user` (best-shot loser, clutter, quality or unmatched reject,
    or the side of an edit pair not kept), `follows` (a Live Photo clip goes with its still),
    or `pending`.
  - `pending` means a question that decides the item is unanswered: a duplicate or best-shot
    group, or a Google creation, which is never imported without a yes.
  - A confirmed duplicate wins over a rejection, and a rejection over a pending question.
- **Batches:** importable items not imported yet, a Live Photo's still and clip as one asset,
  `[import] batch_gigabytes` (20) of originals per `import-batch` action.
- **Applying a batch:**
  1. Stream each original out of its zip into a scratch folder.
  2. Write the working copy with exiftool `-o` (the original is only read). It gets
     `date_fixes.planned_write` (ingest's plan plus the review's date) and the approved
     place.
  3. Import the batch into "From Google". Photos' duplicate dialog is skipped, because the
     catalogue already decided.
  4. Set the favourite, caption and keywords (`People/…`, `date-approx`,
     `location-inferred`, `from-google`). Anything exiftool can't write into the file goes
     through Photos instead.
  5. Add each asset to its Google albums, then record the import (catalogue v14 `imports`:
     Photos uuid, working-copy SHA-256, batch). The scratch folder goes with the batch.
  Recorded items are never imported again.
- **Existing iCloud items:** `set-date` for an answered date, and `set-location` (plus
  `location-inferred`) for an approved place. Photos changes them in place.
- **The library** (`importer.library`): `PhotoscriptLibrary` drives Photos.app (photoscript,
  which comes with osxphotos). Photos works on whatever library it has open, so it first
  checks that Photos' open library is the `--library` given, and refuses otherwise. It maps
  imported assets back by file name, which the importer makes unique (`<item id>-<name>`).
- **Tests** never drive Photos.app (CLAUDE.md rule 4). `testing.photos.DisposableLibrary` is
  a folder-backed library that records every asset, album, favourite, caption, keyword, date
  and place. The photoscript adapter is tested against a fake photoscript.
  `tests/macos/test_photos_app_import.py` is the real acceptance test: it runs only when
  `PHOTOMIGRATE_PHOTOS_APP_LIBRARY` names a throwaway library under /tmp that Photos has
  open. Delmar runs it on the Air.

## Review gallery (M3-06)

`photomigrate review [--takeout DIR] [--library PATH] [--port 8765]` serves a local gallery
(`photomigrate.review`: FastAPI, Jinja2, htmx 2.0.4 vendored). It **only records decisions**.
Each answer goes into the catalogue's `decisions` table at once, so a review can stop and
resume at any time. Nothing else changes, and a test compares every other table before and
after.

- **Queues** (`review.queues`), one card per question, each with a proposal (key `a`) and
  other answers:

  | Queue | Decision (subject, kind) | Value |
  |---|---|---|
  | Duplicates | group, `duplicate` | `{"keeper": "source:key"}` or `{"reject": true}` (not duplicates) |
  | Best shot | group, `best-shot` | `{"keep": [...]}`. Pairs keep both and are not asked. |
  | Edited versions | group, `edit-pair` | `{"keep": [Takeout paths]}`. A bulk button applies one policy to every pair. |
  | Dates | group, `date` | the `date_fixes` answers (form: accept, keep, a date, a year, a shift in hours) |
  | Clutter | item, `clutter` | `{"reject": true/false}`. Bulk: reject a whole category. |
  | Quality flags | item, `quality` | `{"reject": true/false}` |
  | Locations | item, `location` | `{"approve": true/false}`, with a map tile |
  | Unmatched | item, `unmatched` | `{"reject": true/false}` (no sidecar, bad sidecar, unpaired motion or edit, original missing) |

  Answers are checked before they're stored. The question must be one the gallery shows,
  and date answers must be ones `date_fixes` can apply. Bulk answers are all-or-nothing.
- **Thumbnails** (`review.thumbs`): Google items are streamed from the Takeout zips, iCloud
  items read from the library's originals (with a `ReadOnlyGuard` on the library while the
  server runs). Photos are decoded as everywhere else (upright, HEIC too); videos get an
  ffmpeg poster frame. The JPEG is at most 320 px, cached in `data/thumbs/` by SHA-256. A file
  that isn't at hand gets a grey placeholder, which is not cached. On the real sample: 68 ms
  from the 4 GB zip, 5 ms cached, 0.27 s for a video poster.
- **Keyboard:** `j`/`k` move between cards, `a` accepts, `r` rejects, `k` keeps, `1`–`9` picks a
  keeper, `b`/`o`/`e` set edit pairs, `s` saves best-shot ticks, `?` shows help. After an
  answer, focus moves to the next unanswered card. Every queue has a progress bar.
- **Who can reach it:** the server binds to `127.0.0.1` only (`server_config`, no option to
  change it; a test starts it and checks the socket). Requests naming another host are
  refused (DNS rebinding). Requests that write need a same-origin `Origin` header, so another
  web site open in the browser can't record answers. The location map's tile is the one
  outside request: the browser fetches it from tile.openstreetmap.org, with no referrer.

## Location backfill (M3-05)

`photomigrate analyse [--timeline Timeline.json] [--timezone ZONE]` runs `locations`
(`analyse.locations`) after clutter.

- **Neighbours:** a shot without GPS borrows from the same camera's located shots within
  `[location] window_minutes` (30), by wall-clock time. Between one shot before and one after,
  the place is interpolated in time (`interpolated`); with one, it is copied (`neighbour`).
  The nearest shot on each side counts. Shots without a camera borrow nothing.
  `any_device = true` lets every camera's shots count (two phones at one event).
- **Timeline** (`photomigrate.timeline`, only when no neighbour helped): Delmar's export is the
  iOS on-device format, a list of `visit`, `activity` and `timelinePath` segments with
  `geo:lat,lon` strings. A moment inside a visit takes the shortest such visit
  (`timeline-visit`). Otherwise it is interpolated between the path and activity points just
  before and after it, if both are within `timeline_tolerance_minutes` (15), or takes the one
  that is (`timeline-path`). Local times are read in `--timezone` (default: this Mac's). The
  real export parses: 692 visits and 7,869 points, from November 2025 to October 2026.
- **Proposals only** (catalogue v13 `inferred_locations`: place, method, evidence). The review
  approves each one with the item's `location` decision `{"approve": true}`. The importer
  writes approved places (`approved_location`) with the keyword `location-inferred`. Stale
  proposals are retired.
- **Left out:** items with their own GPS, Google edits, creations and Live Photo motion clips,
  proposed duplicates, and the clutter queue.
- **Config:** `true`/`false` settings are now allowed (`any_device`); anything else is an
  error.
- **On the real sample:** 557 iPhone 4S photos have no GPS at all, and all 735 iPhone 5 photos
  do. So "same device" proposes nothing. `any_device` would propose 56 at 30 minutes (28 at
  5 minutes, 168 at 2 hours). The Timeline export starts in 2025 and the sample is from
  2013–2014, so it doesn't help there.

## Clutter (M3-04)

`photomigrate analyse` runs `clutter` (`analyse.clutter`) after best-shot. Each item gets a
score from 0 to 1 per category. If its best category reaches `[clutter] min_score` (0.5), it
joins the **clutter queue** (catalogue v12 table `clutter`: category, score, evidence) for bulk
review. Nothing is decided.

- **Measured at ingest and index** (catalogue v12 `items.text`): Vision's
  `VNDetectTextRectanglesRequest` finds text (without reading it) in the decode already made
  for hashing. It's stored as a summary (`text_regions`): `regions`, `coverage` (capped at 1),
  `tallest` (as a share of the height), and `banded` (share of regions in the top or bottom
  30%). Index also records Photos' own `screenshot` flag.
- **Scores** combine clues as independent evidence, `1 − Π(1 − weight)`. A camera make and model
  in the file count against the categories a camera doesn't make:

  | Category | Clues (weight) | Camera |
  |---|---|---|
  | `google-creation` | ingest's `google-creation` (sidecar origin AUTO) 1.0; `-MOTION`, `-MIX`, `-SMILE`, `-ANIMATION`, `-COLLAGE`, `-EFFECTS` names 0.9 | — |
  | `screenshot` | Photos says so 0.95; a screenshot name (8 languages, screen recordings too) 0.8; a device screen size 0.5; PNG 0.3; no camera and no file date 0.2 | × 0.25 |
  | `document` | text regions 0.7 at 12+; coverage 0.5 at 20%; white paper with 3+ regions 0.3; without Vision text, zero-shot 0.9 × p; no camera 0.15, only on top of other evidence | — |
  | `meme` | big captions at the top or bottom 0.6; some text over a picture 0.2; a saved name (`received_`, `FB_IMG_`, `image.jpg`…) 0.3; no camera and no file date 0.2; zero-shot 0.8 × p | × 0.2 (and × 0.5 for a page of text) |
  | `forward` | WhatsApp `-WA0001` 0.85; other messengers (`received_`, `FB_IMG_`, `signal-`, Telegram) 0.6; stripped metadata 0.2; a messaging size (long side 800…4096) 0.25 | × 0.3 |

  Photographed receipts stay documents: a camera doesn't count against them. A missing camera
  never makes a document by itself, and a street sign in a camera photo stays a photo (a
  little text: one region, 0.5% coverage).
- **Zero-shot fallback** (no Vision text, e.g. Linux): `ZeroShot` compares the stored open_clip
  embedding with prompts for document, meme, screenshot and ordinary photos (softmax at
  scale 100). Each label's probability is the sum of its prompts'. The prompts are embedded
  once, and only if some item needs them, so on macOS the model never loads for this.
- **Left out:** Google edits and Live Photo motion clips (they follow their original or still)
  and proposed duplicates. Google creations are always queued, never imported automatically
  (Delmar).
- **Fixtures** (`testing.media`): `text_page` (a receipt), `captioned` (a meme), `with_sign`
  (a street sign in a photo). On macOS the real Vision runs over the same Takeout as the Linux
  test, which uses the summaries Vision gives.
- **On the real sample** (2026-10-20, re-ingested read-only into /tmp in 4 min 55 s; two
  cameras, iPhone 4S and 5, and 197 items without one):
  - The first rules queued 138 Google creations (Delmar expected 130 or more), 86 documents
    and 32 screenshots. 74 of the documents were a single "text" box 57 to 79% of the
    picture's height: Vision boxing a textured scene. 23 of the screenshots were 1080p camera
    videos.
  - The fixes: drop boxes taller than 30% of the picture (no line of text is), count coverage
    and paper only from 3 regions up, and match a screen size only for photos, at a weight that
    needs a second clue.
  - After the fixes (estimated from the stored summaries): 138 creations, 7 documents (the
    surest with 8 to 11 text regions) and 7 screenshots, out of 3,308 items. No memes or
    forwards: the album has none of their names.

## Date audit (M3-03)

`photomigrate analyse` ends with `date-fixes` (`analyse.date_fixes`), then `date-audit`
(`analyse.date_audit`). The audit asks; the fixes apply the review's answers.

- **What is audited:** every item, Google and iCloud, except Google edits, creations and Live
  Photo motion clips (they follow their original or still, or go to clutter), and photos
  proposed as another's duplicate. An item's current date: the user's answer, else (iCloud)
  the Photos date, else (Google) the file's own date unless it's a placeholder, else the date
  ingest plans to write.
- **Reasons** (an item can have several):

  | Reason | When |
  |---|---|
  | `missing` | no date at all |
  | `placeholder` | the file or Photos says 1970-01-01, 1980-01-01 or 2000-01-01, or only a placeholder sidecar date is left |
  | `future` | after today (a day's grace for local times) |
  | `upload-as-taken` | no file date, and the sidecar's capture time is its upload time |
  | `conflict` | two capture clues (file, sidecar capture time, Photos date, file name) more than a day apart; upload times and mtimes are never clues; a local time and an instant a whole offset apart agree |
  | `out-of-sequence` | the camera's shots numbered just before and after agree with each other, and it is more than a day outside them |

- **Sequences:** one source, one camera (catalogue v11 `items.camera`, from EXIF or QuickTime
  make and model), one name prefix (`IMG_`, `DSC`, `P`…), in shot-number order. `(N)` and edit
  suffixes are ignored.
- **File-name dates** (`ingest.names.date_from_filename`, Hypothesis round trips): camera,
  Pixel, Android and macOS screenshots (12-hour too), WhatsApp, Telegram, Signal, Dropbox, and
  bare dates (read as midnight).
- **Batches** (`groups(kind='date-batch')`): flagged items by problem (`undated` or
  `doubtful`), camera, folder or album (iCloud items: "iCloud Photos"), and name prefix. The key
  names the batch, not its members, so a stored answer stays attached when a batch grows.
  Members are in shot order with their reasons as role. Details: problem, camera, folder,
  prefix, each member's current date and suggestion, and the batch offset.
- **Suggestions** per item: the date in its name; else interpolated by shot number between the
  nearest well-dated shots of its sequence (or the one on its side); else a plausible sidecar
  capture time. A doubtful batch whose members are all off their suggestions by the same amount
  (to the minute) suggests that **offset** (a wrong camera clock).
- **Answers** (the review stores one per batch as the group's `date` decision; M3-06):
  `accept` (each item's suggestion), `keep`, `date` (one date for all), `year` (1 July at noon,
  keyword `date-approx`; noon keeps the day in any zone), `shift` (`offset_seconds`, exact,
  keeping each item's UTC offset). Entered dates are local wall-clock time.
- **Fixes:** one `fix-date` (Google) or `plan-timewarp` (iCloud, run by osxphotos timewarp in
  M4) action per item. Either way the date becomes the item's `user` candidate, and
  `date-approx` is its keyword exactly when the date is a year guess. Ingest's metadata plan is
  left alone: the importer writes a working copy from `date_fixes.planned_write`, which puts the
  answered date (in the home zone, if it is a bare local time) over ingest's plan. Answers are
  worked out from the dates the batch was shown with (the `user` candidate ignored), so a shift
  is never applied twice. An answer that cannot work becomes a `bad-answer` action; the other
  batches still apply. Answered items are never flagged again.
- **On the real sample** (one shared album; the /tmp catalogue predates camera data): 1 batch
  of 3. Two shots are 3 to 5 days out of their neighbours' sequence, and one is the
  months-apart conflict. The 20 other EXIF-vs-sidecar conflicts are all under 14 hours, below
  the 1-day rule, and the 171 items dated from their sidecar have a plausible date.

## Best shot (M3-02)

`photomigrate analyse` runs `best-shot` (`analyse.best_shot`) last, after dedupe and quality.

- **Measured at ingest and index**, from the decode already made for hashes and metrics
  (catalogue v10, `items.embedding` and `items.faces`; `NULL` means not measured):
  - **embedding** (`photomigrate.embedding`): open_clip `ViT-B-32` with the
    `laion2b_s34b_b79k` weights, a unit vector of 512 values stored as float16 (1 KB). The
    weights (about 600 MB) are downloaded on first use; the model runs on MPS when there is
    one. Vectors from different models can't be compared, so a model change means measuring
    again.
  - **faces** (`photomigrate.faces`, macOS): Vision's `VNDetectFaceCaptureQualityRequest` gives
    each face a capture quality (0 to 1). Vision has no eye-state classifier, so Core Image's
    `CIDetector` (`CIDetectorEyeBlink`) says whether the eyes are closed; its faces are
    matched to Vision's by centre. A picture Vision can't read is left unmeasured rather than
    stopping ingest. On Linux there is no face reader and `faces` stays `NULL`.
- **Who takes part:** photos with an embedding and a capture time (EXIF, else the Photos date,
  else the date ingest plans to write). Left out: Google edits and creations, and photos
  proposed as another's duplicate (dedupe keeps one copy; best-shot ranks that one).
- **Groups:** shots in time order; each joins the open group holding the shot it is most like,
  if that cosine similarity is at least `min_similarity`, else it starts a group. A group is
  open for `window_seconds` (default 120) after its first shot, so all its pairs are within
  the window, and a long series of one view splits into several groups. Times compare as
  wall-clock time (any offset dropped): shots of one moment come from one camera.
- **Scores** (each 0 to 1), and their **weights** (config `[best_shot.weights]`):

  | Score | What | Weight |
  |---|---|---|
  | `sharpness` | sharpness over the sharpest in the group | 0.3 |
  | `exposure` | 1 − distance of the mean from mid-grey − clipped share | 0.15 |
  | `faces` | mean Vision capture quality (0 with no face) | 0.2 |
  | `eyes_open` | share of faces with both eyes open (unknown counts ½; 0 with no face) | 0.2 |
  | `aesthetic` | Apple's overall aesthetic score (iCloud only) | 0.15 |

  A shot's score is the weighted mean of the scores **every** shot in its group has, so a group
  is always compared on the same terms. The face scores count only if every shot was read for
  faces and one has a face, so without Vision (Linux) ranking falls back to the other scores.
- **Keep the top two** (Delmar's decision, a constant, not a setting). Ties go to higher
  resolution, then iCloud, then the key, so the ranking doesn't depend on input order.
- **Stored as proposals:** `groups(kind='best-shot')`, members `keep` / `to-delete` in rank
  order, with a Live Photo still's clip right after it as `motion`. Details: the scores used,
  each shot's score and parts, and the two kept. Pairs are stored too (both `keep`), so review
  can show them. Stale groups are retired; no `decisions` are written; review (M3-06) confirms.
- **Tests** never download the weights. `ClipEmbedder` runs with random weights; grouping
  tests use `testing.embedder.ThumbnailEmbedder` (a centred 16x16 thumbnail), and an autouse
  fixture gives the CLI that stand-in. Vision is tested on osxphotos' test library photos
  (a portrait, a group, a blink, tulips).
- **Linux CI** installs torch's CPU wheels (`[tool.uv.sources]`), not the CUDA ones.
- **On the real sample** (2026-10-20, read-only, into /tmp; 3,282 photos, 1,306 of them
  eligible after edits and duplicates):
  - Ingest took 4 min 43 s instead of 80 s: CLIP (MPS) and Vision add about 60 ms a photo.
  - Faces: 3,273 photos have at least one (it is a people album), 4,586 faces in all. Core
    Image calls 17% of them closed-eyed, and that goes with Vision's quality: 33% below 0.3,
    12% from 0.3 to 0.5, none above 0.5.
  - Similarity within 2 minutes is a continuum (5,993 pairs, most from 0.86 to 0.96), so the
    fine pHash was used as a proxy for "same framing" (distance ≤ 100 of 256). Pairs sharing
    their framing: 10–26% from 0.86 to 0.92, about 40% from 0.92 to 0.94, 64–68% from 0.94 to
    0.96, 80% and more above 0.96. Below 0.94 the links are mostly other compositions of the
    same event, so **`min_similarity` defaults to 0.94**.
  - Proposed for "To Delete" by threshold: 661 at 0.90, 507 at 0.92, 351 at 0.94, 242 at 0.95,
    142 at 0.96. At 0.94: 216 groups (88 pairs; the largest has 18), 432 kept. Every group was
    ranked on sharpness, exposure, faces and eyes (no Apple scores in a Google-only sample).
    16% of the kept shots have a closed-eye face, against 27% of the proposed ones.
  - The threshold still needs Delmar's eye in the review gallery (M3-06).

## Quality flags (M3-01)

Ingest and index measure every item once, while the file is at hand (`photomigrate.quality`,
stored as JSON in `items.quality`, catalogue v9). Then `photomigrate analyse` runs `quality`
(`analyse.quality`), which only compares the measurements with the thresholds, so trying new
thresholds is quick.

- **Photos** are decoded once for both their hashes and their metrics (`hashing.open_picture`:
  upright, long side at most 1024 px; JPEGs are decoded at reduced size).
  - `sharpness`: the variance of the Laplacian. Low means blurry.
  - `brightness`: the mean luminance. `dark_fraction` and `bright_fraction`: the share of
    pixels below 16 and above 240.
  - `contrast`: the luminance standard deviation. Near 0 is a flat frame.
  - `megapixels` and `short_side`, from the full upright size.
- **Videos** reuse the 8 frames sampled for the signature (one ffmpeg pass): mean
  `brightness`, and `motion`, the mean change between consecutive samples.
- **Flags**, with the default thresholds (`photomigrate.example.toml`):

  | Flag | When |
  |---|---|
  | `blurry` | sharpness < 30 |
  | `low-resolution` | < 0.3 MP |
  | `under-exposed` | brightness < 40, or more than half the pixels near black |
  | `over-exposed` | brightness > 215, or more than half the pixels near white |
  | `accidental` | contrast < 10 (pocket shot, black frame, lens cap) |
  | `short-clip` | < 1.5 s, never a Live Photo motion half (paired or `unpaired-motion`) |
  | `black-video` | brightness < 12 |
  | `static-video` | motion < 2 |
  | `tiny-video` | short side < 240 px |

- **Scores:** each flag has a score from 0 (just past the threshold) to 1 (as bad as it gets),
  in the `quality_flags` table. A re-run with other thresholds replaces an item's flags.
  Nothing is decided: the review gallery shows the flags, and best-shot (M3-02) can use them.
- **Config:** `--config FILE` (TOML). Unknown sections or settings, wrong types and negative
  values are errors, so a typo never silently falls back to a default.
- **On the real sample** (3,282 photos, 26 videos):
  - Sharpness spans 15 to 6,000 (median 170). The usual default of 100 would flag a quarter of
    the photos; 30 flags 29 (0.9%).
  - Nothing else is flagged. Mean brightness stays between 44 and 175, contrast is at least 30,
    the smallest photo is 640x480, and every video is 6.6 s or longer, with plenty of motion.
  - Decoding once for hashes and metrics left dedupe unchanged: the same 258 duplicate groups
    and 1,562 edit pairs, member for member.

## Edited versions (M2-04)

`photomigrate analyse` first runs `edited-versions` (`analyse.edited`). It links every Google
item flagged `edited` (localised and cut suffixes included) to its original in the same folder.
The rule is the sidecar matcher's `original_of`: same stem and `(N)`, exact extension first.
Album copies resolve to their one item.

- **Storage:** each link is an `edit-pair` group (catalogue v8 rebuilds `groups` to allow the
  new kind; migrations run with foreign keys off, then checked). Members are `original`
  (rank 0) and `edited` (rank 1). Details hold the `policy` and `keep`.
- **Policies** (`--edited-policy`):
  - `pair`, the default: keep both;
  - `original-only`;
  - `edited-only`.
  Switching the policy updates every group. The importer reads `keep`.
- **Orphans:** an edit without its original is flagged `unpaired-edit`. Originals get `has-edit`.
- **Dedupe:** edits never take part, so an edit is never proposed as a duplicate of its original.
- **On the real sample:** 1,562 edit pairs and no orphans. Photo dedupe found 256 groups in that
  one shared album, 200 of them `IMG_1234.JPG` + its re-upload `IMG_1234_2.jpg`. The members are
  visually identical (256-bit distance 0–3) and share the EXIF second.

## Video dedupe (M2-03)

`photomigrate analyse` also runs `dedupe-videos` (`analyse.dedupe_video`).

- **Signatures:** ingest and index store a signature per video (catalogue v7
  `video_signature`, `bitrate`): pHashes of 8 frames sampled evenly by one ffmpeg pass (64x64
  greyscale). The bitrate is size × 8 / duration.
- **Matching:** same SHA-256 (`exact`), or a `near` match. A near match needs:
  - durations within ±0.5 s (only that window is compared);
  - a mean aligned-frame distance of 8 bits or less;
  - an aspect ratio within 2%;
  - the same moment, if both carry a QuickTime date.
  In tests, re-encoded or downscaled copies come out ≤ 4 apart, and different clips of the same
  length > 12 apart.
- **Exclusions:** Live Photo motion clips (`live-photo-motion`, `unpaired-motion`).
- **Keeper order:** resolution, then bitrate, then metadata, then iCloud, then the key.
- **Shared plumbing:** groups go through `analyse.grouping`. Each stage owns the `duplicate`
  groups with its key prefix (`dup-` photos, `vdup-` videos) and retires only its own stale ones.

## Photo dedupe (M2-02)

`photomigrate analyse [--apply]` runs `dedupe-photos` (`analyse.dedupe_photo`) over every
catalogued photo, Google and iCloud. Excluded: Google `edited` copies (M2-04 links them) and
`google-creation`s (clutter queue). Thresholds were measured on generated scenes:

| Pair | 64-bit pHash | 256-bit pHash (`phash_fine`) |
|---|---|---|
| resized / recompressed (q25) / HEIC↔JPEG copy | ≤ 4 | ≤ 8 |
| 95% crop | from 2 | ≥ 18 |
| 90% crop | from 2 | ≥ 54 |
| the same scene a moment later (shapes moved 1%) | from 0 | from 8 |

Rules, all configurable (`DedupeConfig`, CLI flags):
- **Edges:** same SHA-256 (`exact`), or same Live Photo `content_id` (`content-id`), or a
  `near` match. A near match needs:
  - 64-bit pHash ≤ 8 (candidate search: numpy brute force with `bitwise_count`, about 50 s per
    200k photos);
  - then 256-bit pHash ≤ 12, or ≤ 6 when either photo lacks an EXIF time (`fine_max_untimed`);
  - dHash ≤ 10;
  - aspect ratio within 2% (rotation ignored);
  - if both carry an EXIF time, **the exact same second**. Mixed local-vs-UTC comparisons
    allow a whole time-zone offset.
- **Groups are stars around the keeper (`grouping.star_groups`):** every member must match the
  keeper *directly*, so chains (A~B~C with A ≠ C) can't pull different pictures together.
  `method` says how the members match the keeper.
- **Keeper order:** resolution, then metadata (date, GPS, caption), then iCloud, then the key.
- **Stored as proposals only:** `groups(kind='duplicate')` with member roles `keeper` /
  `duplicate`, plus `motion` for the clip of a Live Photo still. Stale groups are retired. No
  `decisions` are written; review (M3-06) confirms.

**Validated on the real sample with pixels** (2026-10-20). Each proposed pair was decoded,
orientation-corrected and compared by SSIM/PSNR; the measurement scripts live outside the repo.
- **The first rules** (1 s tolerance, transitive groups): 270 of 287 pairs were the same image,
  and 17 were different shots. 13 of those were 1 s apart, 3 were chained, and 1 lacked EXIF.
- **Measurements behind the fixes:**
  - all 244 true duplicate pairs with EXIF on both sides shared the exact second;
  - true duplicates without EXIF were within 2 bits on the 256-bit hash;
  - the different shot was 10 bits away.
- **After the fixes:** 276 of 276 pairs are the same image (254 at full size, 22 smaller re-saves
  with SSIM@128 ≥ 0.983).
- **Recall:** of 2,258 loosely similar pairs left ungrouped, 2,256 are different pictures. The
  other 2 share the EXIF second but are 14 bits apart: likely burst frames.
- **Control:** each keeper against its nearest-in-time photo outside the group gave 256 of 257
  "different".

## iCloud index (M2-01)

`photomigrate index <Library.photoslibrary> [--apply]` (`index.icloud`, macOS only, osxphotos).

**Nothing in the real library is written, not even SQLite bookkeeping.** A WAL-mode SQLite
reader updates the database's `-shm` file even with `mode=ro`, and tests caught osxphotos
changing its mtime. So osxphotos reads a **shadow library**: a temporary folder holding a
private clone of `database/` (an APFS clone via `cp -c`, else a copy) and read-only symlinks to
`originals/`, `resources/` and the rest. Paths come back through the symlinks and are mapped to
the real library. A `safety.ReadOnlyGuard` on the real library is active for the whole run.
It refuses write-mode opens, read-write sqlite connections, renames, removals, mkdir, chmod,
utime, copies *into* the library, hard links, symlinks created inside it, and exiftool writes
(`announce_write`).

Per asset (catalogue v5 adds `scores` and `timezone`):
- the Photos uuid, original filename, the original's path relative to the library;
- SHA-256 and pHash/dHash (`hashing`, orientation-aware) of the original;
- the Photos date with its offset (date candidate `icloud`) and embedded EXIF/QuickTime dates;
- GPS, size, video duration and Live Photo content id (from exiftool);
- favourite, caption, albums (`albums`, source `icloud`), keywords, and Apple's 27 scores;
- flags: `edited`, `live-photo`, `hidden`, `burst`, `shared`, `cloud-asset`.

Originals that aren't on this Mac become `index-missing` actions: the item is flagged
`original-missing`, has no hashes, and is listed by the CLI. Quit Photos while indexing, so the
cloned database is a consistent snapshot.

Tests (`tests/macos`, marked `macos`) use osxphotos' own test libraries (Test-27.0_DevBeta and
Test-Live-15.7.2), fetched once at a pinned commit into the system temp dir. Each test gets
its own APFS clone. They prove the library is byte- and mtime-identical after a dry run and an
apply, and that the guard saw zero writes.

## Ingest (M1-04)

`photomigrate ingest <takeout-dir> [--apply]` catalogues every Google Photos item **straight
from the zip parts**: no extraction, and nothing is ever written to the zips. The real export
is about 600 GB (12 × 50 GB) on the MacBook Air's external drive.

- `ingest.takeout`: lists the parts, or an already unzipped `Takeout/` tree. Only
  `Takeout/Google Photos/` is used; other files are counted and ignored. It rejects unsafe
  member paths (zip slip) and parts that disagree about the same file. `local_copy()` streams
  one entry into a scratch file and hashes it on the way. The original's location is
  `<part>::<member>`, relative to the Takeout folder, so the catalogue can move between
  machines.
- `ingest.names` and `ingest.sidecars`: parse names (`(N)` index, edit suffixes including ones
  cut by the 47-character limit, dates in names) and match media to sidecars folder by folder.
  Every candidate pair gets a score: exact 3, cut-but-a-prefix 2, legacy without the media
  extension 1. Pairs are assigned best first, each sidecar to one original at most. Edited
  copies take their original's sidecar: the original with the same stem and index, preferring
  the exact extension, then the same extension ignoring case. Two originals that differ only in
  extension case, plus an edit with its case flipped, cannot be told apart, and the matcher then
  picks the exact-extension one. Every media file gets a `Match`. Unclaimed sidecars become
  orphans.
- `ingest.exif`: a typed PyExifTool wrapper. Every path is announced to the safety guard first.
  `write()` always creates a *new* file (`-o`). Photos get EXIF `DateTimeOriginal` and
  `OffsetTimeOriginal`, GPS, and `XMP-dc:Description`. GIFs get the XMP equivalents. Videos get
  `QuickTime:CreateDate` and `Keys:CreationDate`, `Keys:GPSCoordinates` and `Keys:Description`.
- `ingest.policy`: the file's own date wins. A plausible sidecar date fills a gap, written in the
  home time zone (`--timezone`, by default the Mac's) and flagged `offset-assumed`. Placeholder
  dates, dates after the upload, and the upload time used as a capture date are never written,
  only flagged. Disagreements become `date-conflict`. GPS and caption come from the sidecar when
  missing or different. Every date candidate is recorded (exif or quicktime, sidecar-taken,
  sidecar-created, filename, mtime).
- `ingest.stage`: one `ingest` action per media file, plus `flag-orphan-sidecar` and
  `flag-unknown-file`. Apply hashes the item, reads its metadata, and stores the item, its flags
  (match method, edited, google-creation, policy flags) and its **metadata plan** (catalogue v2
  column `metadata_plan`). Working copies are written at import time (M4-01) for approved items.

### Albums, copies, favourites, captions, people (M1-05)

- **An album is a folder with a `metadata.json`** (the name is localised). That rule doesn't
  depend on the export language. Year folders have no such file. A broken `metadata.json`
  falls back to the folder name.
- **Plain folders are ingested first.** So an item's main record is its `Photos from YYYY` file
  when one exists. Items that live only in albums, like Delmar's shared album, still get one.
- **A later file with the same SHA-256 and the same Google Photos URL** (from its sidecar) is the
  *same item*. It's stored in `item_copies`, and its folder adds an `album_members` row. Same
  bytes but a different URL means a separate Google item, flagged
  `same-bytes-as-another-item`. `catalogue.item_for_key()` maps any Takeout path to its one item,
  which is the basis for reconciliation (M4-03).
- **Favourites and captions** are on `items` (`favorite`, `description`). Face-tag names become
  `People/<name>` keywords (`keywords` table, replaced as a set per prefix). Catalogue v3 adds
  `albums`, `album_members`, `item_copies`, `keywords` and `items.google_url`.

### Live Photos (M1-06)

`photomigrate ingest` runs a second stage, `live-photos`, over the catalogue
(`ingest.livephotos`):

- **Stills** are HEIC/HEIF/JPEG files that are neither edits nor Google creations. **Motion
  candidates** are MOV/MP4 clips of 3.5 s or less.
- **Pairing order:** Apple's `ContentIdentifier` first (catalogue v4 `items.content_id`).
  Exiftool reads it from the still's Apple MakerNotes or from the clip's `Keys:ContentIdentifier`.
  Then the same folder and name stem (including `(N)`) with capture times at most 3 s apart
  (`dates.moment_gap`, which allows for offset-less EXIF). If a time is unknown, the name
  alone decides. Closest pairs win, and each file pairs once.
- **Storage:** pairs become `live-pair` groups (key = the still's path, members still/motion),
  and the items are flagged `live-photo-still` / `live-photo-motion`.
- **Unpaired candidates** are clips that carry an identifier or have a same-named still, but no
  partner. They're flagged `unpaired-motion` for review. Other short clips are ordinary videos.
- **Fixture limit:** fixtures embed the identifier only in the clip. exiftool can't create
  Apple MakerNotes, so the still's copy can't be faked.

## Catalogue and stage runner (M1-03)

### Catalogue (`photomigrate.catalogue`)

SQLite at `data/catalogue.sqlite` (gitignored). The schema version is `PRAGMA user_version`.
`MIGRATIONS` is an ordered tuple numbered 1, 2, 3…, and each migration runs in a single
transaction that also bumps the version. A catalogue newer than the code is refused.
Add a migration for any schema change, and never edit a shipped one.

Tables (v1):

| Table | Holds |
|---|---|
| `items` | one row per Google or iCloud item, identified by `(source, source_key)` (the Takeout path or Photos UUID): paths, sha256, perceptual hashes, size, duration, GPS, title, description, favourite, flags (a JSON array) |
| `date_candidates` | one value per `(item, source)` (`user`: the review's answer, M3-03). Sources: `exif`, `quicktime`, `sidecar-taken`, `sidecar-created`, `filename`, `mtime`, `icloud`, `user`. Naive ISO values mean local time with an unknown offset. |
| `groups`, `group_members` | `duplicate`, `best-shot`, `date-batch`, `live-pair`, `edit-pair` groups, with a member role and rank |
| `decisions` | the user's answers per item or group and kind (import, keep, date…) |
| `stage_runs`, `actions` | the action log: every applied run and each action's key, status and error |

Writes are idempotent. Upserts change a row, and its `updated_at`, only when a value actually
differs.

### Runner (`photomigrate.runner`)

A stage implements `plan(catalogue) -> Iterable[Action]`, which writes nothing, and
`apply(action, catalogue)`, which must be idempotent. `run_stage`:

- **dry run** (the default): plans against `Catalogue.snapshot()`, an in-memory copy (the file is
  opened read-only, or the catalogue starts empty if there is no file), then saves the plan to
  `data/plans/<stage>-<time>-dry-run.json`. That plan file is the only side effect.
- **`--apply`**: skips actions already applied by earlier runs (matched by `Action.key`, a hash
  of kind, target and details, so a changed input yields a new key). It applies each remaining
  action in its own transaction together with its log entry, and stops at the first failure.
  The next run resumes from the failed action. When nothing is pending, nothing is written,
  not even a run record.
- **Undone actions:** an applied action is done only if no action on the same target that the
  stage no longer plans was applied after it. So a setting changed and then changed back
  (`--edited-policy pair`, `original-only`, `pair`) re-applies the first action instead of
  leaving the middle setting in place.

`render_result` prints a summary table, the first 50 actions and the next step.


### Media guard (`photomigrate.safety.PathGuard`, CLAUDE.md rule 4)

A PEP 578 audit hook installed once per process. While a guard is active it inspects
filesystem audit events (`open`, `os.*`, `shutil.*`, `glob.*`, `pathlib.*`, `tempfile.*`,
`sqlite3.connect`) and process launches (`subprocess.Popen`, `os.system`, `os.exec`,
`os.posix_spawn`, `os.spawn`, with argv tokens and `--opt=path` values checked). A protected
path raises `ForbiddenPathError` *before* the operating system sees the call, and the
violation is recorded.

Protected (`GuardRules`):

- always: `~/Pictures` for both the passwd home and `$HOME`, plus `PHOTOMIGRATE_GUARD_FORBID`
  entries;
- unless under an allowed root (tmp, `--basetemp`, `tests/fixtures/`): any path component
  matching `*.photoslibrary`, `Takeout`, `takeout-*.zip` or `takeout-*.tgz` (case-insensitive).

Absolute paths are checked as written and with symlinks resolved. A relative path is judged
only by whether the cwd itself is protected. `shutil.rmtree` and `os.fwalk` emit bare names
relative to a directory fd, which was itself checked when it was opened, so joining those
names to the cwd gives false hits (a tmp `data-in` read as `<repo>/data-in`). The known gap
is cwd-relative access into a protected root from its parent, so tests use absolute paths.

The pytest plugin also forbids `<rootdir>/data-in/`, where Delmar's real Takeout and Timeline
samples live.

The pytest plugin `photomigrate.testing.pytest_guard` (loaded from `tests/conftest.py`)
activates a guard for the session. It fails the test phase (setup, call or teardown) that
caused a violation, even when the code swallowed the exception, and it fails the session for
violations that happen outside any test.

**Rule for later stages:** paths handed to an external tool without appearing in its argv
(PyExifTool's stay-open mode writes them to stdin) must first go through
`photomigrate.safety.announce_path(path)`, so the guard sees them.

### No-delete check (`photomigrate-no-delete-check`, CLAUDE.md rule 3)

An AST scan of `src/` that flags calls to, and imports of, names whose words include
`delete`, `trash`, `unlink`, `rmtree`, `rmdir`, `removedirs` or `send2trash`, plus `remove`
followed by asset/photo/media/item/file, plus `os.remove`. The phrase "to delete" (the
"To Delete" album) is allowed. Strings are not scanned, so SQL `DELETE FROM` on catalogue
rows is fine.

M4-02 closed the known gaps (scripts, `getattr`, moves and renames): see "To Delete" above.
