from typer.testing import CliRunner

from photomigrate import __version__
from photomigrate.cli import app

runner = CliRunner()


def test_version_flag_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"photomigrate {__version__}"
