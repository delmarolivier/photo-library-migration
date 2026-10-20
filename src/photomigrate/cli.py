"""Command-line entry point: `photomigrate`."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
from rich.console import Console

from photomigrate import __version__
from photomigrate.analyse.best_shot import BestShotStage
from photomigrate.analyse.date_audit import DateAuditStage
from photomigrate.analyse.date_fixes import DateFixStage
from photomigrate.analyse.dedupe_photo import DedupeConfig, DedupeStage
from photomigrate.analyse.dedupe_video import VideoDedupeConfig, VideoDedupeStage
from photomigrate.analyse.edited import EditedVersionsStage
from photomigrate.analyse.quality import QualityStage
from photomigrate.config import Config, ConfigError, load_config
from photomigrate.embedding import default_embedder
from photomigrate.faces import face_reader
from photomigrate.index.icloud import IndexStage, LibraryError
from photomigrate.ingest.livephotos import LivePhotoStage
from photomigrate.ingest.policy import local_zone
from photomigrate.ingest.stage import IngestStage
from photomigrate.ingest.takeout import TakeoutError
from photomigrate.runner import (
    DEFAULT_CATALOGUE,
    DEFAULT_PLAN_DIR,
    Action,
    StageFailedError,
    render_result,
    run_stage,
)
from photomigrate.text_regions import text_reader

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
) -> None:
    """Link edits to originals, propose duplicate groups (photos and videos) with keepers,
    flag quality problems with scores (thresholds from --config), group similar shots,
    keeping the best two of each (window, similarity and weights from --config), apply the
    review's answers about dates, and batch the dates still in doubt for the review.

    Proposals only: the review decides.
    """
    settings: Settings = ctx.obj
    console = Console()
    stages = (
        EditedVersionsStage(edited_policy.value),
        DedupeStage(
            DedupeConfig(candidate_phash=candidate_phash, fine_max=fine_max, dhash_max=dhash_max)
        ),
        VideoDedupeStage(VideoDedupeConfig(signature_max=signature_max)),
        QualityStage(settings.config.quality),
        BestShotStage(settings.config.best_shot),
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


_STAGES = {
    "review": "Local review gallery; decisions are stored in the catalogue.",
    "import": "Import approved items into Photos (dry-run unless --apply).",
    "report": "Reconciliation report: every Google item accounted for.",
}


def _register_stub(stage: str, help_text: str) -> None:
    def stub() -> None:
        typer.echo(f"'{stage}' is not implemented yet (see Linear P-CLA-3).", err=True)
        raise typer.Exit(code=1)

    app.command(stage, help=help_text)(stub)


for _stage, _help in _STAGES.items():
    _register_stub(_stage, _help)
