# Progress log

## Current state
- Milestone: 1. Prep and ingest
- Last completed issue: [M1-06] CLA-78 (Live Photo pairing). Milestone 1's code is complete.
- Next issue: [M2-01] CLA-79 (iCloud library indexer, read-only). This needs the Air with the
  Photos library on the external drive, Full Disk Access for `sshd`, and Delmar's go-ahead.
- Coverage: 100% line + branch (gate 90%)
- Open questions for Delmar:
  - The external APFS drive (2 TB or more) for the MacBook Air: still to be attached. Then
    download the 12 Takeout parts onto it before the Google links expire.

## Session log
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
