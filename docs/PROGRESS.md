# Progress log

## Current state
- Milestone: 1. Prep and ingest
- Last completed issue: [M1-01] CLA-73 (repo bootstrap, tooling, CI, safety guard)
- Next issue: [M1-02] CLA-74 (synthetic Google Takeout fixture generator)
- Coverage: 100% line + branch (gate 90%)
- Open questions for Delmar:
  - Which personal Mac will do the real run? Development happens on the work MacBook (fixtures
    only, no personal media). M2-01 real indexing and the M4 import need a Mac signed into
    Delmar's Apple ID with "Download Originals" on and roughly 150 GB free.
  - How should the real Takeout samples in `data-in/` be used? (Added by Delmar 2026-10-20:
    3 zips, 3.7 GB, gitignored and blocked by the media guard.) Proposed: as a reference for
    the M1-02 fixture generator now; an opt-in, local-only dry-run smoke test after M1-04.
  - If that Mac is short on disk: S3 in a *personal* AWS account as temporary storage for the
    Takeout zips (the project's stated fallback). Not needed for M1.

## Session log
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
