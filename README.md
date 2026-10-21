# photo-library-migration

Merge Google Photos into iCloud Photos as one library: dedupe photos and videos, flag
low-quality shots, review every decision in a local gallery, then import. **The tool never
deletes anything**: removals go into a "To Delete" album that you clear by hand.

Status: milestone 4. Every stage works: `ingest`, `index`, `analyse`, `review`, `import` and
`report`. What's left is the real-data rehearsal: see [docs/RUNBOOK.md](docs/RUNBOOK.md).
Plan and task queue: Linear project "Photo Library Migration" (P-CLA-3, ClawWorks).

## Requirements

- macOS for the `index` and `import` stages (they talk to Photos); everything else also runs on Linux.
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for you).
- `brew install exiftool ffmpeg`

## Usage

```sh
uv sync
uv run photomigrate --version
uv run photomigrate --help
```

```sh
# Plan first (prints a table, saves data/plans/ingest-…-dry-run.json, changes nothing)
uv run photomigrate ingest /Volumes/Photos/takeout
# Then catalogue every item, reading straight from the takeout-*.zip parts
uv run photomigrate ingest /Volumes/Photos/takeout --apply --timezone Europe/London
```

`ingest` and `index` also embed every photo with open_clip (for best-shot). The model's
weights (about 600 MB) are downloaded into the Hugging Face cache the first time; on Apple
silicon it runs on the GPU. On macOS they also read faces and text with Apple Vision.

```sh
# Read-only index of the iCloud Photos library (macOS; quit Photos first)
uv run photomigrate index "/Volumes/Photos/Photos Library.photoslibrary"
uv run photomigrate index "/Volumes/Photos/Photos Library.photoslibrary" --apply
```

```sh
# Link edits, propose duplicate groups, flag quality problems, pick best shots,
# audit dates and apply the review's date answers (dry run, then --apply)
uv run photomigrate analyse
uv run photomigrate --config photomigrate.toml analyse --apply
```

```sh
# Review gallery on http://127.0.0.1:8765 (this Mac only); answers are saved as you go
uv run photomigrate review --takeout /Volumes/Photos/takeout \
    --library "/Volumes/Photos/Photos Library.photoslibrary"
```

```sh
# Import approved items (dry run first; --apply needs a confirmed backup and a yes)
uv run photomigrate import --takeout /Volumes/Photos/takeout
uv run photomigrate confirm-backup
uv run photomigrate import --takeout /Volumes/Photos/takeout \
    --library "/Volumes/Photos/Photos Library.photoslibrary" --apply
```

```sh
# Reconciliation: data/report/report.html and .csv; exits 1 while anything is unaccounted
uv run photomigrate report
```

Global options: `--catalogue PATH` (default `data/catalogue.sqlite`), `--plan-dir PATH`
(default `data/plans`) and `--config FILE`. The config file is TOML and only needs the
settings it changes; [`photomigrate.example.toml`](photomigrate.example.toml) lists them all
with their defaults. Unknown settings are errors.

| Stage | What it does |
|---|---|
| `ingest` | Catalogue every item straight from the zips: match sidecars, hash, measure quality, embed photos, read faces, record dates and a metadata plan, albums (copies become one item), favourites, captions, `People/` keywords |
| `index` | Read-only index of the iCloud library: hashes, quality, embeddings, faces, dates, albums, keywords, Apple scores; lists originals that are only in iCloud |
| `analyse` | Link Google edits to originals (`--edited-policy pair|original-only|edited-only`); photo and video dedupe with proposed keepers; quality flags with scores (blurry, badly exposed, accidental, short or static clips…, thresholds in the config file); best shot: similar shots of one moment grouped, the top two kept and the rest proposed for "To Delete" (window, similarity and score weights in the config file); date audit: missing, placeholder, future, conflicting and out-of-sequence dates batched by camera, folder and name, with suggestions, and the review's answers applied (accept, a date, a year, a shift); clutter queue: screenshots, receipts and documents, memes, messaging forwards and Google creations, each with a score and its evidence; location backfill proposals from the same camera's shots nearby in time or a Google Maps Timeline export (`--timeline`), written only once approved |
| `review` | Local gallery on 127.0.0.1 with one queue per question (duplicates, best shot, edited versions, dates, clutter, quality, locations, unmatched); thumbnails from the zips and the library; keyboard shortcuts; every answer saved at once, nothing else changed |
| `import` | Dry run by default; `--apply` imports approved Google items in batches into "From Google" (albums, favourites, captions, keywords, Live Photos as pairs) and applies answered dates and approved places to existing iCloud items; puts the iCloud items the review rejected in "To Delete" (never deletes); resumable, never imports an item twice |
| `report` | Reconciliation (HTML and CSV): every Google item imported, a duplicate of one that stays, or rejected in review; totals and bytes; the SHA-256 trail; fixes and To Delete counts; exits 1 while anything is unaccounted |

Every stage that writes is a **dry run by default**. It prints the plan as a table and saves it
as JSON under `data/plans/`, then stops. Add `--apply` to carry the plan out. Stages are
resumable and safe to re-run: a finished stage has nothing left to do.

To download the Takeout parts onto an external drive one at a time, see
`scripts/download-takeout.sh --help` and RUNBOOK step 0.

## Development

Strict TDD with a 90% line + branch coverage gate. See [CLAUDE.md](CLAUDE.md) for the rules.

```sh
uv run pre-commit install
uv run pytest -n auto --cov --cov-branch --cov-fail-under=90 && uv run ruff check && uv run ruff format --check && uv run mypy src
```

Safety rails (details in [docs/DESIGN.md](docs/DESIGN.md)):

- **Media guard**: every test session runs under an audit hook that fails any test touching
  `~/Pictures`, a `*.photoslibrary` outside tmp, or a real Takeout export. Extra paths to protect
  (for example where your real Takeout lives) go in `PHOTOMIGRATE_GUARD_FORBID`.
- **Personal data stays out of git**: real Takeout samples live in `data-in/` and the catalogue in
  `data/`. Both are gitignored, blocked by a pre-commit hook, and checked in CI.
- **No-delete check**: `uv run photomigrate-no-delete-check src` fails if production code calls a
  delete or trash API, looks one up by name, scripts Photos to delete, or moves a file. It runs
  in pre-commit and CI. At runtime, `import --apply` keeps the Takeout read-only and refuses
  any removal in the library, and a test runs the whole pipeline with every delete API rigged
  to fail.

CI (GitHub Actions): `linux` and `no-delete-check` on every PR and on `main`; `macos` and the
combined `coverage` gate on PRs only. `main` is protected and only accepts squash-merged PRs.
