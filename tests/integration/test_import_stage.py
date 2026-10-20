"""Importing into a disposable Photos library: approved items only, once, with their details."""

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from photomigrate.analyse.dedupe_photo import DedupeStage
from photomigrate.analyse.edited import EditedVersionsStage
from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from photomigrate.config import ImportConfig
from photomigrate.importer.stage import FROM_GOOGLE, ImportStage
from photomigrate.ingest.exif import ExifTool
from photomigrate.ingest.livephotos import LivePhotoStage
from photomigrate.ingest.stage import IngestStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.media import synthetic_photo
from photomigrate.testing.photos import DisposableLibrary
from photomigrate.testing.takeout import Geo, TakeoutBuilder

ROOT = "Takeout/Google Photos"
YEAR = "Photos from 2021"
WHEN = datetime(2021, 6, 1, 12, 0, tzinfo=UTC)
ZONE = ZoneInfo("Europe/Paris")


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, Path, DisposableLibrary]:
    b = TakeoutBuilder(seed=21)
    beach = b.photo(YEAR, "IMG_0001.JPG", taken=WHEN, description="Plage", favorited=True,
                    people=["Ann"], geo=Geo(43.7, 7.26), image=synthetic_photo(1))  # fmt: skip
    b.copy_to(beach, "Holiday")
    b.album("Holiday", description="Summer holiday")
    b.photo(
        YEAR, "IMG_0002.JPG", taken=WHEN, exif_taken=None, sidecar=None, image=synthetic_photo(2)
    )
    b.live_photo(YEAR, "IMG_0003", taken=WHEN)
    b.photo(YEAR, "IMG_0004.JPG", taken=WHEN, image=synthetic_photo(4))  # rejected in review
    b.photo(YEAR, "IMG_0005.JPG", taken=WHEN, image=synthetic_photo(5))  # already in iCloud
    b.write_zips(tmp_path / "takeout")
    path = tmp_path / "catalogue.sqlite"
    common = {"catalogue_path": path, "plan_dir": tmp_path / "plans", "apply": True}
    with IngestStage(tmp_path / "takeout", tmp_path / "work", zone=ZONE) as stage:
        run_stage(stage, **common)  # type: ignore[arg-type]
    for step in (LivePhotoStage(), EditedVersionsStage()):
        run_stage(step, **common)  # type: ignore[arg-type]
    library = DisposableLibrary(tmp_path / "Test.photoslibrary")
    with Catalogue.open(path) as catalogue:
        five = catalogue.get_item("google", f"{ROOT}/{YEAR}/IMG_0005.JPG")
        assert five is not None
        existing = catalogue.upsert_item(
            CatalogueItem(source="icloud", source_key="UUID-5", kind="photo", sha256=five.sha256,
                          phash=five.phash, dhash=five.dhash, phash_fine=five.phash_fine)
        )  # fmt: skip
        library.add_existing("UUID-5", "IMG_0005.JPG")
    run_stage(DedupeStage(), **common)  # type: ignore[arg-type]
    with Catalogue.open(path) as catalogue:
        (dup,) = catalogue.groups("duplicate")
        catalogue.set_decision("group", dup.id, "duplicate", {"keeper": "icloud:UUID-5"})
        four = catalogue.item_for_key("google", f"{ROOT}/{YEAR}/IMG_0004.JPG")
        catalogue.set_decision("item", four or 0, "quality", {"reject": True})
        two = catalogue.item_for_key("google", f"{ROOT}/{YEAR}/IMG_0002.JPG") or 0
        catalogue.set_date_candidate(two, "user", datetime(2004, 7, 1, 12, 0))
        catalogue.set_keywords(two, "date-approx", ["date-approx"])
        assert existing
    return path, tmp_path / "takeout", library


def run_import(
    setup: tuple[Path, Path, DisposableLibrary], tmp_path: Path, *, apply: bool = True,
    config: ImportConfig | None = None,
) -> RunResult:  # fmt: skip
    path, takeout, library = setup
    with ImportStage(
        library, takeout, tmp_path / "work", zone=ZONE, config=config or ImportConfig()
    ) as stage:
        return run_stage(stage, catalogue_path=path, plan_dir=tmp_path / "plans", apply=apply)


def by_file(library: DisposableLibrary) -> dict[str, dict[str, object]]:
    return {asset["files"][0].split("-", 1)[1]: {**asset, "uuid": uuid}
            for uuid, asset in library.assets().items() if "-" in asset["files"][0]}  # fmt: skip


def test_approved_google_items_are_imported_into_from_google(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    run_import(setup, tmp_path)

    _, _, library = setup
    imported = by_file(library)
    assert set(imported) == {"IMG_0001.JPG", "IMG_0002.JPG", "IMG_0003.HEIC"}
    assert sorted(library.albums()[FROM_GOOGLE]) == sorted(a["uuid"] for a in imported.values())  # type: ignore[type-var]


def test_albums_favourites_captions_and_keywords_come_along(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    run_import(setup, tmp_path)

    _, _, library = setup
    beach = by_file(library)["IMG_0001.JPG"]
    assert (beach["favorite"], beach["description"]) == (True, "Plage")
    assert beach["keywords"] == ["People/Ann", "from-google"]
    assert library.albums()["Holiday"] == [beach["uuid"]]
    assert by_file(library)["IMG_0002.JPG"]["keywords"] == ["date-approx", "from-google"]


def test_a_live_photo_is_imported_as_one_asset_with_its_clip(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    run_import(setup, tmp_path)

    _, _, library = setup
    live = by_file(library)["IMG_0003.HEIC"]
    assert [name.split("-", 1)[1] for name in live["files"]] == ["IMG_0003.HEIC", "IMG_0003.MOV"]  # type: ignore[union-attr]


def test_working_copies_carry_the_planned_metadata_and_the_zips_are_untouched(
    setup,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    takeout = setup[1]
    before = {z.name: z.read_bytes() for z in takeout.glob("*.zip")}

    run_import(setup, tmp_path)

    _, _, library = setup
    copy = next((library.root / "originals").rglob("*-IMG_0002.JPG"))
    with ExifTool() as tool:
        assert tool.read(copy).taken == datetime(2004, 7, 1, 12, 0, tzinfo=ZONE)
    assert {z.name: z.read_bytes() for z in takeout.glob("*.zip")} == before
    assert not list((tmp_path / "work").rglob("*.JPG")), "working copies are not left behind"


def test_each_import_is_recorded_with_its_uuid_and_the_working_copys_hash(
    setup,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    run_import(setup, tmp_path)

    path, _, library = setup
    with Catalogue.open(path) as catalogue:
        records = catalogue.imports()
        motion = catalogue.item_for_key("google", f"{ROOT}/{YEAR}/IMG_0003.MOV")
        still = catalogue.item_for_key("google", f"{ROOT}/{YEAR}/IMG_0003.HEIC")
    assert len(records) == 4
    assert records[motion or 0][0] == records[still or 0][0], "a Live Photo is one asset"
    assert all(uuid in library.assets() for uuid, _, _ in records.values())
    assert all(len(sha) == 64 for _, sha, _ in records.values())


def test_a_second_run_imports_nothing_new(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    run_import(setup, tmp_path)
    before = setup[2].assets()

    again = run_import(setup, tmp_path)

    assert again.plan.actions == ()
    assert setup[2].assets() == before


def test_a_dry_run_changes_nothing_in_the_library(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    state = (setup[2].root / "library.json").read_bytes()

    result = run_import(setup, tmp_path, apply=False)

    assert result.plan.summary() == {"import-batch": 1}
    assert (setup[2].root / "library.json").read_bytes() == state


def test_imports_go_in_batches_of_the_configured_size(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    result = run_import(setup, tmp_path, config=ImportConfig(batch_gigabytes=1e-9))

    assert result.plan.summary() == {"import-batch": 3}, "a Live Photo's pair stays together"
    assert len(by_file(setup[2])) == 3


def test_answered_dates_and_approved_places_reach_existing_icloud_items(
    setup,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    path, _, library = setup
    with Catalogue.open(path) as catalogue:
        uuid = catalogue.item_for_key("icloud", "UUID-5") or 0
        catalogue.set_date_candidate(uuid, "icloud", datetime(2000, 1, 1, tzinfo=UTC))
        catalogue.set_date_candidate(uuid, "user", datetime(2012, 5, 6, 7, 8))
        catalogue.set_inferred_location(uuid, (48.85, 2.35), "neighbour", {})
        catalogue.set_decision("item", uuid, "location", {"approve": True})

    result = run_import(setup, tmp_path)

    assert {"set-date", "set-location"} <= set(result.plan.summary())
    existing = library.asset("UUID-5")
    assert (existing["date"], existing["location"]) == ("2012-05-06T07:08:00", [48.85, 2.35])
    assert existing["keywords"] == ["location-inferred"]
    assert run_import(setup, tmp_path).plan.actions == ()


def test_pending_questions_hold_items_back(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    path = setup[0]
    with Catalogue.open(path) as catalogue:
        (dup,) = catalogue.groups("duplicate")
        catalogue.set_decision("group", dup.id, "duplicate", {"reject": True})
        one = catalogue.item_for_key("google", f"{ROOT}/{YEAR}/IMG_0001.JPG") or 0
        group = catalogue.upsert_group("best-shot", "best-x", {"keep": [], "scores": {}})
        catalogue.set_group_members(group, [(one, "keep", 0)])

    run_import(setup, tmp_path)

    assert set(by_file(setup[2])) == {"IMG_0002.JPG", "IMG_0003.HEIC", "IMG_0005.JPG"}


def test_an_action_for_an_item_that_is_gone_is_an_error(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from photomigrate.runner import Action

    path, takeout, library = setup
    action = Action("import-batch", "batch-x", {"assets": [["GONE", None]], "bytes": 1})
    with (
        ImportStage(library, takeout, tmp_path / "work", zone=ZONE, config=ImportConfig()) as stage,
        Catalogue.open(path) as catalogue,
        pytest.raises(KeyError, match="GONE"),
    ):
        stage.apply(action, catalogue)


# --- the command ---------------------------------------------------------------------------


def cli_args(setup, tmp_path: Path, *extra: str) -> list[str]:  # type: ignore[no-untyped-def]
    path, takeout, library = setup
    return ["--catalogue", str(path), "--plan-dir", str(tmp_path / "plans"), "import",
            "--takeout", str(takeout), "--library", str(library.root), "--work-dir",
            str(tmp_path / "work"), "--timezone", "Europe/Paris",
            "--backup-marker", str(tmp_path / "BACKUP-CONFIRMED"), *extra]  # fmt: skip


@pytest.fixture
def disposable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("photomigrate.cli.photos_library", DisposableLibrary)


def test_the_command_is_a_dry_run_by_default(setup, tmp_path: Path, disposable: None) -> None:  # type: ignore[no-untyped-def]
    result = CliRunner().invoke(app, cli_args(setup, tmp_path))

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert by_file(setup[2]) == {}


def test_applying_needs_a_confirmed_backup(setup, tmp_path: Path, disposable: None) -> None:  # type: ignore[no-untyped-def]
    result = CliRunner().invoke(app, cli_args(setup, tmp_path, "--apply"), input="y\n")

    assert result.exit_code == 1
    assert "backup" in result.output
    assert by_file(setup[2]) == {}


def test_applying_asks_first_and_a_no_imports_nothing(
    setup, tmp_path: Path, disposable: None
) -> None:  # type: ignore[no-untyped-def]
    marker = tmp_path / "BACKUP-CONFIRMED"
    runner = CliRunner()
    confirmed = runner.invoke(app, ["confirm-backup", "--backup-marker", str(marker)], input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert marker.read_text(encoding="utf-8").startswith("Backup confirmed")

    declined = runner.invoke(app, cli_args(setup, tmp_path, "--apply"), input="n\n")
    assert declined.exit_code == 1
    assert by_file(setup[2]) == {}

    accepted = runner.invoke(app, cli_args(setup, tmp_path, "--apply"), input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert len(by_file(setup[2])) == 3


def test_a_backup_not_confirmed_is_not_marked(tmp_path: Path) -> None:
    marker = tmp_path / "BACKUP-CONFIRMED"

    result = CliRunner().invoke(
        app, ["confirm-backup", "--backup-marker", str(marker)], input="n\n"
    )

    assert result.exit_code == 1
    assert not marker.exists()


def test_applying_needs_the_takeout_and_the_library(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    path = setup[0]
    result = CliRunner().invoke(app, ["--catalogue", str(path), "import", "--apply"])

    assert result.exit_code == 2
    assert "--takeout" in result.output


def test_a_library_photos_does_not_have_open_stops_the_import(
    setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    from photomigrate.importer.library import LibraryMismatchError

    def refuse(path: Path) -> object:
        raise LibraryMismatchError("Photos has another library open")

    monkeypatch.setattr("photomigrate.cli.photos_library", refuse)
    (tmp_path / "BACKUP-CONFIRMED").write_text("Backup confirmed", encoding="utf-8")

    result = CliRunner().invoke(app, cli_args(setup, tmp_path, "--apply"), input="y\n")

    assert result.exit_code == 1
    assert "another library open" in result.output
    assert json.loads((setup[2].root / "library.json").read_text())["albums"] == {}


def approve_a_place(setup) -> int:  # type: ignore[no-untyped-def]
    with Catalogue.open(setup[0]) as catalogue:
        two = catalogue.item_for_key("google", f"{ROOT}/{YEAR}/IMG_0002.JPG") or 0
        catalogue.set_inferred_location(two, (45.76, 4.84), "neighbour", {})
        catalogue.set_decision("item", two, "location", {"approve": True})
    return two


def test_an_approved_place_is_written_into_the_working_copy(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    approve_a_place(setup)

    run_import(setup, tmp_path)

    copy = next((setup[2].root / "originals").rglob("*-IMG_0002.JPG"))
    with ExifTool() as tool:
        read = tool.read(copy)
    assert (round(read.latitude or 0, 4), round(read.longitude or 0, 4)) == (45.76, 4.84)
    assert "location-inferred" in by_file(setup[2])["IMG_0002.JPG"]["keywords"]  # type: ignore[operator]


def test_what_cannot_be_written_into_a_file_is_set_in_photos(
    setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    approve_a_place(setup)
    monkeypatch.setattr("photomigrate.importer.stage.can_write", lambda name: False)

    run_import(setup, tmp_path)

    two = by_file(setup[2])["IMG_0002.JPG"]
    assert two["date"] == "2004-07-01T12:00:00+02:00"
    assert two["location"] == [45.76, 4.84]


def test_a_dry_run_stage_cannot_apply(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from photomigrate.runner import Action

    path, takeout, _ = setup
    with (
        ImportStage(None, takeout, tmp_path / "work", zone=ZONE, config=ImportConfig()) as stage,
        Catalogue.open(path) as catalogue,
        pytest.raises(RuntimeError, match="dry run"),
    ):
        stage.apply(Action("set-date", "UUID-5", {"to": "2012-01-01", "keywords": []}), catalogue)


def test_applying_without_a_library_is_refused(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    path, takeout, _ = setup

    result = CliRunner().invoke(
        app, ["--catalogue", str(path), "import", "--takeout", str(takeout), "--apply"]
    )

    assert result.exit_code == 2
    assert "--library" in result.output


def test_the_real_library_is_refused_unless_photos_has_it_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from photomigrate import cli
    from photomigrate.importer import library
    from photomigrate.importer.library import LibraryMismatchError

    monkeypatch.setattr(library, "_open_library", lambda: str(tmp_path / "Other.photoslibrary"))

    with pytest.raises(LibraryMismatchError):
        cli.photos_library(tmp_path / "Test.photoslibrary")
