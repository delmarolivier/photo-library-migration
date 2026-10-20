import pytest
from typer.testing import CliRunner

from photomigrate import __version__
from photomigrate.cli import app

runner = CliRunner()


def test_version_flag_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"photomigrate {__version__}"


STAGES = ["ingest", "index", "analyse", "review", "import", "report"]


def test_help_lists_every_pipeline_stage() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for stage in STAGES:
        assert stage in result.stdout


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])

    assert "Usage" in result.output
    assert "ingest" in result.output


@pytest.mark.parametrize("stage", [s for s in STAGES if s not in ("ingest", "index")])
def test_stub_stage_reports_not_implemented_and_fails(stage: str) -> None:
    result = runner.invoke(app, [stage])

    assert result.exit_code == 1
    assert f"'{stage}' is not implemented yet" in result.stderr
