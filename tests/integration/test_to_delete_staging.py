"""The "To Delete" album holds exactly the iCloud items the review rejected; nothing is deleted."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.config import ImportConfig
from photomigrate.importer.outcomes import to_delete
from photomigrate.importer.stage import TO_DELETE, ImportStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.photos import DisposableLibrary
from photomigrate.testing.takeout import TakeoutBuilder

ZONE = ZoneInfo("UTC")


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, Path, DisposableLibrary, dict[str, int]]:
    TakeoutBuilder(seed=1).write_zips(tmp_path / "takeout")  # an empty Takeout
    library = DisposableLibrary(tmp_path / "Test.photoslibrary")
    path = tmp_path / "catalogue.sqlite"
    ids = {}
    with Catalogue.open(path) as catalogue:
        for uuid in ("KEEP", "COPY", "BEST", "WORSE", "SCREEN", "BLUR", "FINE"):
            ids[uuid] = catalogue.upsert_item(
                CatalogueItem(source="icloud", source_key=uuid, kind="photo", title=f"{uuid}.HEIC")
            )
            catalogue.set_date_candidate(ids[uuid], "icloud", datetime(2020, 1, 1, tzinfo=UTC))
            library.add_existing(uuid, f"{uuid}.HEIC")
        dup = catalogue.upsert_group(
            "duplicate", "dup-1", {"keeper": "icloud:KEEP", "method": "exact"}
        )
        catalogue.set_group_members(
            dup, [(ids["KEEP"], "keeper", 0), (ids["COPY"], "duplicate", 1)]
        )
        catalogue.set_decision("group", dup, "duplicate", {"keeper": "icloud:KEEP"})
        shot = catalogue.upsert_group(
            "best-shot", "best-1", {"keep": ["icloud:BEST"], "scores": {}}
        )
        catalogue.set_group_members(
            shot, [(ids["BEST"], "keep", 0), (ids["WORSE"], "to-delete", 1)]
        )
        catalogue.set_decision("group", shot, "best-shot", {"keep": ["icloud:BEST"]})
        catalogue.set_decision("item", ids["SCREEN"], "clutter", {"reject": True})
        catalogue.set_decision("item", ids["BLUR"], "quality", {"reject": True})
        catalogue.set_decision("item", ids["FINE"], "quality", {"reject": False})
    return path, tmp_path / "takeout", library, ids


def stage(setup, tmp_path: Path, *, apply: bool = True) -> RunResult:  # type: ignore[no-untyped-def]
    path, takeout, library, _ = setup
    with ImportStage(
        library, takeout, tmp_path / "work", zone=ZONE, config=ImportConfig()
    ) as importer:
        return run_stage(importer, catalogue_path=path, plan_dir=tmp_path / "plans", apply=apply)


def test_the_album_holds_exactly_the_rejected_icloud_items(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    result = stage(setup, tmp_path)

    _, _, library, _ = setup
    assert result.plan.summary() == {"stage-to-delete": 4}
    assert sorted(library.albums()[TO_DELETE]) == ["BLUR", "COPY", "SCREEN", "WORSE"]
    with Catalogue.open(setup[0]) as catalogue:
        assert {catalogue.item(i).source_key for i in to_delete(catalogue)} == {
            "BLUR", "COPY", "SCREEN", "WORSE",
        }  # fmt: skip
        assert catalogue.staged() == {
            setup[3][uuid]: reason
            for uuid, reason in [("COPY", "duplicate"), ("WORSE", "best-shot"),
                                 ("SCREEN", "clutter"), ("BLUR", "quality")]
        }  # fmt: skip


def test_staging_deletes_nothing(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    stage(setup, tmp_path)

    assert set(setup[2].assets()) == set(setup[3]), "every asset is still in the library"


def test_a_withdrawn_rejection_leaves_the_album(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    stage(setup, tmp_path)
    with Catalogue.open(setup[0]) as catalogue:
        catalogue.set_decision("item", setup[3]["SCREEN"], "clutter", {"reject": False})

    result = stage(setup, tmp_path)

    assert result.plan.summary() == {"unstage-to-delete": 1}
    assert sorted(setup[2].albums()[TO_DELETE]) == ["BLUR", "COPY", "WORSE"]
    assert "SCREEN" in setup[2].assets()


def test_staging_twice_changes_nothing_and_a_dry_run_nothing_at_all(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    state = (setup[2].root / "library.json").read_bytes()
    assert stage(setup, tmp_path, apply=False).plan.summary() == {"stage-to-delete": 4}
    assert (setup[2].root / "library.json").read_bytes() == state

    stage(setup, tmp_path)

    assert stage(setup, tmp_path).plan.actions == ()
