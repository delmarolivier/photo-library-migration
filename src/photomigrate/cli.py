"""Command-line entry point: `photomigrate`."""

from typing import Annotated

import typer

from photomigrate import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"photomigrate {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    """Merge Google Photos into iCloud Photos. Never deletes anything."""


_STAGES = {
    "ingest": "Unzip Takeout, match sidecars, write metadata to working copies.",
    "index": "Read-only index of the iCloud library.",
    "analyse": (
        "Dedupe, Live Photos, edited versions, quality, best-shot, dates, clutter, location."
    ),
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
