# CLAUDE.md: Photo Library Migration

## Mission
Consolidate Delmar's Google Photos (~30–40 GB) and iCloud Photos (~30–40 GB) into ONE iCloud Photos library.
Remove duplicates and low-quality items, keep the best two shots of similar groups, fix dates,
preserve albums, favourites and captions, and prove every Google item is accounted for. Photos AND videos.

Scope and task queue: Linear project "Photo Library Migration" (P-CLA-3), workspace ClawWorks.
Read the project description and the document "Claude Code Playbook: start here" before starting.

## Non-negotiable rules
1. STRICT TDD. For every change: write a failing test → run it and see it fail → write the minimum code
   to pass → refactor. Never write production code without a failing test first.
   Never skip, weaken, xfail or delete a test to make the suite pass.
2. Coverage ≥ 90% (line + branch), enforced with --cov-fail-under=90. Never lower it.
   `# pragma: no cover` only with a justification comment and a note in docs/PROGRESS.md.
3. NEVER DELETE USER MEDIA. No code path may delete from Photos or from source files.
   Removals = add to the "To Delete" album; Delmar deletes manually. CI enforces this (see M4-02).
4. Tests never touch the real Photos library, ~/Pictures, or real Takeout data. Use generated
   fixtures and a disposable Photos library under a tmp dir. A session-level guard fixture fails the
   run if a real library path is opened.
5. Every command that writes is DRY-RUN BY DEFAULT; real writes need --apply. Dry-run is tested.
6. Real iCloud library access is READ-ONLY until milestone 4, and writes only happen after
   Delmar has approved decisions in the review gallery.
7. Idempotent and resumable: every stage records state in the SQLite catalogue and can be re-run safely.
8. Never commit media, real metadata, catalogue DBs, Takeout data or secrets (.gitignore them).
9. Ask, don't guess: if a requirement is ambiguous, post a question on the Linear issue and in
   docs/PROGRESS.md, then move to the next unblocked issue.
10. Stop and ask Delmar before the FIRST run of anything against his real library or real Takeout data.

## Tech stack
- Python 3.12, uv, src layout, package `photomigrate`, Typer CLI `photomigrate`.
- Tests: pytest, pytest-cov (branch), hypothesis, pytest-xdist.
- Quality: ruff (lint + format), mypy --strict on src/.
- Media: exiftool (PyExifTool), ffmpeg/ffprobe, Pillow + pillow-heif, imagehash, OpenCV, open_clip (CPU/MPS).
- macOS only: osxphotos (read, import, albums, keywords, timewarp), pyobjc Vision (face capture quality, text detection).
  Mark tests @pytest.mark.macos; they run locally and on the macOS CI runner.
- State: SQLite catalogue at data/catalogue.sqlite (gitignored).
- Review gallery: FastAPI + Jinja2 + htmx, bound to 127.0.0.1 only.

## CLI
photomigrate ingest   # unzip Takeout, match sidecars, write metadata to WORKING COPIES
photomigrate index    # read-only index of the iCloud library
photomigrate analyse  # dedupe, live photos, edited versions, quality, best-shot, dates, clutter, location
photomigrate review   # local gallery; decisions stored in catalogue
photomigrate import   # dry-run by default; --apply imports approved items, albums, date fixes, To Delete staging
photomigrate report   # reconciliation report

## Product decisions (from Delmar: do not change without asking)
- iCloud Photos is the single destination library.
- Best-shot picker keeps the TOP TWO shots per similar group.
- Unknown dates on old photos: a YEAR is enough → store 1 July of that year + keyword "date-approx".
- Date problems are ASKED, in batches (camera model / folder / filename sequence), with suggestions
  from neighbouring shots; support shifting a whole batch by an offset.
- Imported Google items go into album "From Google"; removals into album "To Delete".
- Keep Google albums, favourites and descriptions (as captions).
- Edited versions: default keep original + edit as a linked pair; configurable.
- Clutter queue: screenshots, receipts/documents, memes, WhatsApp/messaging forwards.
- Location backfill from neighbouring shots; optional Google Maps Timeline export.
- Reconciliation: every Google item ends as imported | duplicate-of-existing | rejected-by-user. Zero unaccounted.
- Google-made creations (-MOTION.gif, -MIX, -SMILE, composition AUTO) go to the clutter review queue,
  never auto-imported. (2026-10-20)
- Takeout face-tag names (people[].name) become Photos keywords "People/<name>". (2026-10-20)

## Session workflow
1. Read CLAUDE.md, docs/PROGRESS.md, then Linear P-CLA-3.
2. Pick the lowest-numbered unblocked issue in Todo (titles are numbered [M1-01]…). Move it to In Progress.
3. Branch using the issue's gitBranchName.
4. TDD loop; small conventional commits referencing the issue ID.
5. Full check before PR:
   uv run pytest -n auto --cov --cov-branch --cov-fail-under=90 && uv run ruff check && uv run ruff format --check && uv run mypy src
6. gh pr create linking the Linear issue; wait for CI green; squash-merge.
7. Mark the issue Done in Linear with a comment: what changed, tests added, coverage.
8. Update docs/PROGRESS.md (handoff section).
9. Continue with the next issue if context allows; otherwise stop cleanly with the handoff written.

## Definition of done (every issue)
- Every acceptance criterion has at least one test that was written first.
- CI green on ubuntu and macOS; coverage ≥ 90%; ruff and mypy clean.
- README usage and docs/PROGRESS.md updated; Linear issue commented and closed.
