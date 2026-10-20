"""`photomigrate ingest`: a dry run by default, --apply to catalogue, clear errors."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from photomigrate.cli import app
from photomigrate.ingest.stage import IngestStage
from photomigrate.testing.takeout import TakeoutManifest

runner = CliRunner()


def invoke(tmp_path: Path, *args: str) -> object:
    return runner.invoke(
        app,
        [
            "--catalogue", str(tmp_path / "catalogue.sqlite"),
            "--plan-dir", str(tmp_path / "plans"),
            "ingest", *args, "--work-dir", str(tmp_path / "work"), "--timezone", "Europe/Paris",
        ],
    )  # fmt: skip


def test_ingest_is_a_dry_run_by_default(
    standard_takeout_zips: TakeoutManifest, tmp_path: Path
) -> None:
    result = invoke(tmp_path, str(standard_takeout_zips.zips[0].parent))

    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "Dry run" in result.output  # type: ignore[attr-defined]
    assert "ingest" in result.output  # type: ignore[attr-defined]
    assert not (tmp_path / "catalogue.sqlite").exists()
    assert len(list((tmp_path / "plans").glob("ingest-*-dry-run.json"))) == 1


def test_ingest_apply_catalogues_every_item(
    standard_takeout_zips: TakeoutManifest, tmp_path: Path
) -> None:
    result = invoke(tmp_path, str(standard_takeout_zips.zips[0].parent), "--apply")

    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    expected = len(standard_takeout_zips.items) + len(standard_takeout_zips.albums)
    assert f"Applied {expected} actions" in result.output  # type: ignore[attr-defined]
    assert (tmp_path / "catalogue.sqlite").exists()


def test_a_folder_without_a_takeout_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()

    result = invoke(tmp_path, str(tmp_path / "empty"))

    assert result.exit_code == 1
    assert "no takeout" in result.output  # type: ignore[attr-defined]


def test_an_unknown_time_zone_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()

    result = runner.invoke(app, ["ingest", str(tmp_path / "empty"), "--timezone", "Mars/Olympus"])

    assert result.exit_code == 2
    assert "Mars/Olympus" in result.output


def test_a_failing_item_stops_the_run_with_an_error(
    standard_takeout_zips: TakeoutManifest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: IngestStage, action: object, catalogue: object) -> None:
        raise OSError("disk unplugged")

    monkeypatch.setattr(IngestStage, "apply", broken)

    result = invoke(tmp_path, str(standard_takeout_zips.zips[0].parent), "--apply")

    assert result.exit_code == 1
    assert "disk unplugged" in result.output  # type: ignore[attr-defined]


def test_the_default_time_zone_is_this_machines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    (tmp_path / "empty").mkdir()

    result = runner.invoke(
        app, ["--catalogue", str(tmp_path / "c.sqlite"), "ingest", str(tmp_path / "empty")]
    )

    assert result.exit_code == 1
    assert "no takeout" in result.output
