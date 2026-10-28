"""Command-line entry point: `photomigrate`."""

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
from rich.console import Console
from rich.table import Table

from photomigrate import __version__
from photomigrate.analyse.best_shot import BestShotStage
from photomigrate.analyse.clutter import ClutterStage, ZeroShot
from photomigrate.analyse.date_audit import DateAuditStage
from photomigrate.analyse.date_fixes import DateFixStage
from photomigrate.analyse.dedupe_photo import DedupeConfig, DedupeStage
from photomigrate.analyse.dedupe_video import VideoDedupeConfig, VideoDedupeStage
from photomigrate.analyse.edited import EditedVersionsStage
from photomigrate.analyse.highlights import Appeal, HighlightsStage
from photomigrate.analyse.locations import LocationStage
from photomigrate.analyse.quality import QualityStage
from photomigrate.catalogue import Catalogue
from photomigrate.config import Config, ConfigError, load_config
from photomigrate.embedding import default_embedder
from photomigrate.faces import face_reader
from photomigrate.importer.library import LibraryMismatchError, PhotosLibrary
from photomigrate.importer.stage import ImportStage
from photomigrate.index.icloud import IndexStage, LibraryError
from photomigrate.ingest.livephotos import LivePhotoStage
from photomigrate.ingest.policy import local_zone
from photomigrate.ingest.stage import IngestStage
from photomigrate.ingest.takeout import TakeoutError
from photomigrate.report import reconcile, write_csv, write_html
from photomigrate.review.app import create_app, serve
from photomigrate.runner import (
    DEFAULT_CATALOGUE,
    DEFAULT_PLAN_DIR,
    Action,
    StageFailedError,
    render_result,
    run_stage,
)
from photomigrate.safety import ReadOnlyGuard
from photomigrate.text_regions import text_reader
from photomigrate.timeline import TimelineError, load_timeline

DEFAULT_WORK_DIR = Path("data/work")

app = typer.Typer(no_args_is_help=True, add_completion=False)


@dataclass(frozen=True)
class Settings:
    catalogue: Path
    plan_dir: Path
    config: Config


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"photomigrate {__version__}")
        raise typer.Exit


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
    catalogue: Annotated[
        Path, typer.Option("--catalogue", envvar="PHOTOMIGRATE_CATALOGUE", help="SQLite catalogue.")
    ] = DEFAULT_CATALOGUE,
    plan_dir: Annotated[
        Path, typer.Option("--plan-dir", envvar="PHOTOMIGRATE_PLAN_DIR", help="Where plans go.")
    ] = DEFAULT_PLAN_DIR,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            envvar="PHOTOMIGRATE_CONFIG",
            help="TOML settings (see photomigrate.example.toml). Default: built-in values.",
        ),
    ] = None,
) -> None:
    """Merge Google Photos into iCloud Photos. Never deletes anything.

    Every stage that writes is a dry run unless given --apply.
    """
    try:
        settings = load_config(config)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from None
    ctx.obj = Settings(catalogue, plan_dir, settings)


def _zone(name: str | None) -> ZoneInfo:
    if name is None:
        return local_zone()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise typer.BadParameter(f"unknown time zone {name!r}", param_hint="--timezone") from None


@app.command()
def ingest(
    ctx: typer.Context,
    takeout_dir: Annotated[
        Path,
        typer.Argument(
            file_okay=False, help="Folder with the takeout-*.zip parts (or an unzipped Takeout/)."
        ),
    ],
    work_dir: Annotated[
        Path, typer.Option("--work-dir", help="Scratch space for streaming items.")
    ] = DEFAULT_WORK_DIR,
    timezone: Annotated[
        str | None,
        typer.Option("--timezone", help="IANA zone for sidecar-only dates (default: this Mac's)."),
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Catalogue the items (else a dry run).")
    ] = False,
) -> None:
    """Catalogue every Google Photos item straight from the Takeout zips (read-only),
    then pair split Live Photos.

    Photos are also embedded with open_clip (its weights, about 600 MB, are downloaded on
    first use) and, on macOS, read for faces and text with Apple Vision."""
    settings: Settings = ctx.obj
    zone = _zone(timezone)
    console = Console()
    try:
        with IngestStage(
            takeout_dir,
            work_dir,
            zone=zone,
            embedder=default_embedder(),
            faces=face_reader(),
            text=text_reader(),
        ) as stage:
            for step in (stage, LivePhotoStage()):
                result = run_stage(
                    step, catalogue_path=settings.catalogue, plan_dir=settings.plan_dir, apply=apply
                )
                render_result(result, console)
    except (TakeoutError, StageFailedError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def index(
    ctx: typer.Context,
    library: Annotated[
        Path, typer.Argument(help="The .photoslibrary to read. It is never written.")
    ],
    apply: Annotated[
        bool, typer.Option("--apply", help="Catalogue the items (else a dry run).")
    ] = False,
) -> None:
    """Catalogue the iCloud Photos library, read-only: hashes, dates, albums, scores,
    open_clip embeddings, Vision faces and text, screenshots."""
    settings: Settings = ctx.obj
    console = Console()
    try:
        with IndexStage(
            library, embedder=default_embedder(), faces=face_reader(), text=text_reader()
        ) as stage:
            result = run_stage(
                stage, catalogue_path=settings.catalogue, plan_dir=settings.plan_dir, apply=apply
            )
    except (LibraryError, StageFailedError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    render_result(result, console)
    warn_missing_originals([a for a in result.plan.actions if a.kind == "index-missing"], console)


class EditedPolicyChoice(StrEnum):
    pair = "pair"
    original_only = "original-only"
    edited_only = "edited-only"


@app.command()
def analyse(
    ctx: typer.Context,
    apply: Annotated[
        bool, typer.Option("--apply", help="Store the proposals (else a dry run).")
    ] = False,
    candidate_phash: Annotated[
        int, typer.Option(help="Max 64-bit pHash distance to consider a photo pair.")
    ] = DedupeConfig.candidate_phash,
    fine_max: Annotated[
        int, typer.Option(help="Max 256-bit pHash distance for a near-duplicate photo.")
    ] = DedupeConfig.fine_max,
    dhash_max: Annotated[
        int, typer.Option(help="Max dHash distance for a near-duplicate photo.")
    ] = (DedupeConfig.dhash_max),
    signature_max: Annotated[
        float, typer.Option(help="Max mean frame distance for a near-duplicate video.")
    ] = VideoDedupeConfig.signature_max,
    edited_policy: Annotated[
        EditedPolicyChoice, typer.Option(help="What to keep of an original and its Google edit.")
    ] = EditedPolicyChoice.pair,
    timeline: Annotated[
        Path | None,
        typer.Option(help="A Google Maps Timeline export (iOS format) for location backfill."),
    ] = None,
    timezone: Annotated[
        str | None,
        typer.Option("--timezone", help="IANA zone of local times (default: this Mac's)."),
    ] = None,
) -> None:
    """Link edits to originals, propose duplicate groups (photos and videos) with keepers,
    flag quality problems with scores (thresholds from --config), group similar shots,
    keeping the best two of each (window, similarity and weights from --config), queue
    clutter (screenshots, documents, memes, forwards, Google creations), keep the best share
    of each year's photos across its events (highlights, from --config), apply the review's
    answers about dates, and batch the dates still in doubt for the review.

    Proposals only: the review decides.
    """
    settings: Settings = ctx.obj
    console = Console()
    zone = _zone(timezone)
    try:
        places = None if timeline is None else load_timeline(timeline)
    except TimelineError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    stages = (
        EditedVersionsStage(edited_policy.value),
        DedupeStage(
            DedupeConfig(candidate_phash=candidate_phash, fine_max=fine_max, dhash_max=dhash_max)
        ),
        VideoDedupeStage(VideoDedupeConfig(signature_max=signature_max)),
        QualityStage(settings.config.quality),
        BestShotStage(settings.config.best_shot),
        ClutterStage(settings.config.clutter, ZeroShot(default_embedder())),
        HighlightsStage(
            settings.config.highlights,
            settings.config.best_shot.weights,
            appeal=Appeal(default_embedder()),
        ),
        LocationStage(settings.config.location, places, zone),
        DateFixStage(),
        DateAuditStage(now=datetime.now(UTC)),
    )
    try:
        for stage in stages:
            result = run_stage(
                stage, catalogue_path=settings.catalogue, plan_dir=settings.plan_dir, apply=apply
            )
            render_result(result, console)
    except StageFailedError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


DEFAULT_THUMBS = Path("data/thumbs")
DEFAULT_BACKUP_MARKER = Path("data/BACKUP-CONFIRMED")


def photos_library(path: Path) -> PhotosLibrary:
    """The real library, through Photos.app (macOS). Refuses unless Photos has it open."""
    from photomigrate.importer.library import PhotoscriptLibrary

    return PhotoscriptLibrary(path)


BackupMarker = Annotated[
    Path, typer.Option("--backup-marker", help="The file that says the backup was checked.")
]


@app.command("confirm-backup")
def confirm_backup(backup_marker: BackupMarker = DEFAULT_BACKUP_MARKER) -> None:
    """Say that the Photos library is backed up, which `import --apply` needs."""
    if not typer.confirm(
        "Is there a full copy of the Photos library (made with Photos quit, after its last "
        "change, with every original downloaded), on another disk or folder?"
    ):
        typer.echo("Not marked. Check the backup first.", err=True)
        raise typer.Exit(code=1)
    backup_marker.parent.mkdir(parents=True, exist_ok=True)
    backup_marker.write_text(f"Backup confirmed {datetime.now(UTC).isoformat()}\n", "utf-8")
    typer.echo(f"Backup confirmed: {backup_marker}")


@app.command("import")
def import_(
    ctx: typer.Context,
    takeout: Annotated[
        Path, typer.Option(file_okay=False, help="The Takeout folder (zips are only read).")
    ],
    library: Annotated[
        Path | None,
        typer.Option(help="The .photoslibrary Photos has open; needed with --apply."),
    ] = None,
    work_dir: Annotated[
        Path, typer.Option("--work-dir", help="Scratch space for one batch's working copies.")
    ] = DEFAULT_WORK_DIR,
    timezone: Annotated[
        str | None,
        typer.Option("--timezone", help="IANA zone of local times (default: this Mac's)."),
    ] = None,
    backup_marker: BackupMarker = DEFAULT_BACKUP_MARKER,
    apply: Annotated[bool, typer.Option("--apply", help="Import (else a dry run).")] = False,
) -> None:
    """Import approved Google items into the album "From Google" with their albums,
    favourites, captions and keywords; apply answered dates and places to existing iCloud
    items; put the iCloud items the review rejected in "To Delete" (never deleted here).
    A dry run unless --apply; --apply needs a confirmed backup and a yes."""
    settings: Settings = ctx.obj
    zone = _zone(timezone)
    console = Console()
    if apply and library is None:
        raise typer.BadParameter("--apply needs the library", param_hint="--library")
    common = {"catalogue_path": settings.catalogue, "plan_dir": settings.plan_dir}
    with ImportStage(None, takeout, work_dir, zone=zone, config=settings.config.importer) as plan:
        dry = run_stage(plan, **common, apply=False)  # type: ignore[arg-type]
    render_result(dry, console)
    if not apply:
        return
    if not backup_marker.is_file():
        typer.echo(
            f"Error: no confirmed backup ({backup_marker}). Copy the Photos library (Photos "
            "quit), then run: photomigrate confirm-backup",
            err=True,
        )
        raise typer.Exit(code=1)
    if not typer.confirm(f"Import into {library} (Photos must have it open)?"):
        typer.echo("Nothing imported.", err=True)
        raise typer.Exit(code=1)
    try:
        photos = photos_library(cast(Path, library))
        with ImportStage(
            photos, takeout, work_dir, zone=zone, config=settings.config.importer
        ) as stage:
            render_result(run_stage(stage, **common, apply=True), console)  # type: ignore[arg-type]
    except (LibraryMismatchError, StageFailedError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def review(
    ctx: typer.Context,
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1.")] = 8765,
    takeout: Annotated[
        Path | None, typer.Option(file_okay=False, help="The Takeout folder, for thumbnails.")
    ] = None,
    library: Annotated[
        Path | None, typer.Option(help="The .photoslibrary, for thumbnails (read-only).")
    ] = None,
    thumbs: Annotated[Path, typer.Option(help="Where thumbnails are cached.")] = DEFAULT_THUMBS,
) -> None:
    """Local review gallery on 127.0.0.1: answer each queue; answers go to the catalogue at
    once. Nothing is imported or deleted here."""
    settings: Settings = ctx.obj
    gallery = create_app(settings.catalogue, thumbs=thumbs, takeout=takeout, library=library)
    typer.echo(f"Review gallery: http://127.0.0.1:{port} (Ctrl-C to stop).")
    with ReadOnlyGuard(library) if library is not None else nullcontext():
        serve(gallery, port)


DEFAULT_REPORT = Path("data/report")


@app.command()
def report(
    ctx: typer.Context,
    out: Annotated[
        Path, typer.Option(help="Where report.csv and report.html go.")
    ] = DEFAULT_REPORT,
) -> None:
    """Reconciliation: every Google item imported, a duplicate of one that stays, or
    rejected in review. Exits 1 while anything is unaccounted."""
    settings: Settings = ctx.obj
    with Catalogue.snapshot(settings.catalogue) as catalogue:  # read-only
        result = reconcile(catalogue)
    write_csv(result, out / "report.csv")
    write_html(result, out / "report.html")
    console = Console()
    table = Table(title="Reconciliation")
    for column in ("Source", "Status", "Items", "Bytes"):
        table.add_column(column)
    for source, rows in result.totals().items():
        for status, (count, size) in rows.items():
            table.add_row(source, status, str(count), f"{size:,}")
    console.print(table)
    console.print(
        f"Date fixes {result.date_fixes}, location fixes {result.location_fixes}, "
        f"staged in To Delete {result.staged}. Report: {out / 'report.html'}"
    )
    console.print(f"{result.imported_assets} assets in From Google (compare with Photos).")
    gaps = result.unaccounted
    console.print(f"{len(gaps)} unaccounted of {len(result.lines)} Google items.")
    for line in gaps[:20]:
        console.print(f"  {line.path}: {line.detail}")
    if len(gaps) > 20:
        console.print(f"  … and {len(gaps) - 20} more (see the report).")
    if gaps:
        raise typer.Exit(code=1)


def warn_missing_originals(missing: list[Action], console: Console, limit: int = 20) -> None:
    """List the items whose originals are only in iCloud, so they are not silently skipped."""
    if not missing:
        return
    verb = "original is" if len(missing) == 1 else "originals are"
    console.print(
        f"[yellow]Warning:[/] {len(missing)} {verb} not on this Mac. Turn on Photos > "
        "Settings > iCloud > Download Originals, then index again:"
    )
    for action in missing[:limit]:
        console.print(f"  {action.details['filename']} ({action.target})")
    if len(missing) > limit:
        console.print(f"  … and {len(missing) - limit} more (see the plan JSON).")
