# photo-library-migration

Merge Google Photos into iCloud Photos as one library: dedupe photos and videos, flag
low-quality shots, review every decision in a local gallery, then import. **The tool never
deletes anything**: removals go into a "To Delete" album that you clear by hand.

Status: milestone 1. `ingest` works; the other stages are stubs.
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

Global options: `--catalogue PATH` (default `data/catalogue.sqlite`) and `--plan-dir PATH`
(default `data/plans`).

| Stage | What it does |
|---|---|
| `ingest` | Catalogue every item straight from the zips: match sidecars, hash, record dates and a metadata plan, albums (copies become one item), favourites, captions, `People/` keywords |
| `index` | Read-only index of the iCloud library |
| `analyse` | Dedupe, Live Photos, edited versions, quality, best-shot, dates, clutter, location |
| `review` | Local gallery on 127.0.0.1; decisions stored in the catalogue |
| `import` | Dry-run by default; `--apply` imports approved items and stages removals |
| `report` | Reconciliation report: every Google item accounted for |

Every stage that writes is a **dry run by default**. It prints the plan as a table and saves it
as JSON under `data/plans/`, then stops. Add `--apply` to carry the plan out. Stages are
resumable and safe to re-run: a finished stage has nothing left to do.

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
  delete or trash API. It runs in pre-commit and CI.

CI (GitHub Actions): `linux` and `no-delete-check` on every PR and on `main`; `macos` and the
combined `coverage` gate on PRs only. `main` is protected and only accepts squash-merged PRs.
