"""`photomigrate report` after a fixture run: every item accounted for, or a non-zero exit."""

import csv
from pathlib import Path

from typer.testing import CliRunner

from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from tests.integration.test_import_stage import ROOT, YEAR, run_import, setup  # noqa: F401


def report(path: Path, out: Path) -> tuple[int, str, dict[str, dict[str, str]]]:
    result = CliRunner().invoke(app, ["--catalogue", str(path), "report", "--out", str(out)])
    with (out / "report.csv").open(encoding="utf-8", newline="") as stream:
        rows = {row["path"].rsplit("/", 1)[-1]: row for row in csv.DictReader(stream)}
    return result.exit_code, result.output, rows


def test_the_report_matches_the_fixture_run_exactly(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    run_import(setup, tmp_path)
    path, _, library = setup
    before = path.read_bytes()

    code, output, rows = report(path, tmp_path / "report")

    assert code == 0, output
    assert {name: row["status"] for name, row in rows.items()} == {
        "IMG_0001.JPG": "imported", "IMG_0002.JPG": "imported", "IMG_0003.HEIC": "imported",
        "IMG_0003.MOV": "imported", "IMG_0004.JPG": "rejected-by-user",
        "IMG_0005.JPG": "duplicate-of-existing",
    }  # fmt: skip
    assert (rows["IMG_0004.JPG"]["detail"], rows["IMG_0005.JPG"]["detail"]) == ("quality", "UUID-5")
    imported = {row["photos_uuid"] for row in rows.values() if row["status"] == "imported"}
    assert imported <= set(library.assets())
    assert rows["IMG_0003.HEIC"]["photos_uuid"] == rows["IMG_0003.MOV"]["photos_uuid"]
    assert rows["IMG_0001.JPG"]["copies"] == "1", "its Holiday album copy"
    assert all(len(r["working_sha256"]) == 64 for r in rows.values() if r["status"] == "imported")
    assert "0 unaccounted" in output
    assert "0 unaccounted" in (tmp_path / "report" / "report.html").read_text(encoding="utf-8")
    assert path.read_bytes() == before, "the report only reads the catalogue"


def test_a_planted_unaccounted_item_fails_the_report(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    run_import(setup, tmp_path)
    path = setup[0]
    with Catalogue.open(path) as catalogue:
        catalogue.upsert_item(
            CatalogueItem(source="google", source_key=f"{ROOT}/{YEAR}/planted.jpg", kind="photo")
        )

    code, output, rows = report(path, tmp_path / "report")

    assert code == 1
    assert rows["planted.jpg"]["status"] == "unaccounted"
    assert "1 unaccounted" in output
    assert "planted.jpg" in output


def test_a_long_list_of_gaps_is_cut_short_on_screen(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    path = setup[0]
    with Catalogue.open(path) as catalogue:
        for n in range(25):
            catalogue.upsert_item(
                CatalogueItem(
                    source="google", source_key=f"{ROOT}/{YEAR}/gap{n:02d}.jpg", kind="photo"
                )
            )

    code, output, rows = report(path, tmp_path / "report")

    assert code == 1
    assert "more (see the report)" in output
    assert sum(1 for r in rows.values() if r["status"] == "unaccounted") >= 25


def test_the_report_gives_the_asset_count_to_compare_with_photos(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    run_import(setup, tmp_path)

    code, output, _ = report(setup[0], tmp_path / "report")

    assert code == 0
    assert "3 assets in From Google" in output
    assert "3 assets in From Google" in (tmp_path / "report" / "report.html").read_text("utf-8")
