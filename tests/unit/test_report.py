"""The reconciliation report: one status per Google item, totals, fixes, and no gaps."""

import csv
from datetime import datetime
from pathlib import Path

import pytest

from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.report import Line, reconcile, write_csv, write_html

ROOT = "Takeout/Google Photos/Photos from 2021"


@pytest.fixture
def cat(tmp_path: Path) -> Catalogue:
    catalogue = Catalogue.open(tmp_path / "catalogue.sqlite")
    yield catalogue  # type: ignore[misc]
    catalogue.close()


def google(cat: Catalogue, name: str, size: int = 100, **changes: object) -> int:
    return cat.upsert_item(
        CatalogueItem(source="google", source_key=f"{ROOT}/{name}", kind="photo",
                      sha256=name.encode().hex().ljust(64, "0")[:64], size=size).replace(**changes)
    )  # fmt: skip


def icloud(cat: Catalogue, uuid: str, size: int = 1000) -> int:
    return cat.upsert_item(CatalogueItem(source="icloud", source_key=uuid, kind="photo", size=size))


def line(cat: Catalogue, name: str) -> Line:
    return next(entry for entry in reconcile(cat).lines if entry.path == f"{ROOT}/{name}")


def duplicate(cat: Catalogue, keeper: int, copy: int, *, answer: bool = True) -> None:
    keeper_name = f"{cat.item(keeper).source}:{cat.item(keeper).source_key}"
    group = cat.upsert_group("duplicate", f"dup-{copy}", {"keeper": keeper_name, "method": "exact"})
    cat.set_group_members(group, [(keeper, "keeper", 0), (copy, "duplicate", 1)])
    if answer:
        cat.set_decision("group", group, "duplicate", {"keeper": keeper_name})


def test_an_imported_item_shows_its_uuid_and_its_sha256_trail(cat: Catalogue) -> None:
    item = google(cat, "IMG_0001.JPG")
    cat.record_import(item, "UUID-A", "f" * 64, "batch-000001")

    found = line(cat, "IMG_0001.JPG")

    assert (found.status, found.detail, found.photos_uuid) == ("imported", "UUID-A", "UUID-A")
    assert (found.sha256, found.working_sha256) == (cat.item(item).sha256, "f" * 64)


def test_a_duplicate_of_an_icloud_item_names_that_item(cat: Catalogue) -> None:
    copy = google(cat, "IMG_0002.JPG")
    duplicate(cat, icloud(cat, "UUID-OLD"), copy)

    assert (line(cat, "IMG_0002.JPG").status, line(cat, "IMG_0002.JPG").detail) == (
        "duplicate-of-existing", "UUID-OLD",
    )  # fmt: skip


def test_a_duplicate_of_an_imported_google_item_names_its_new_uuid(cat: Catalogue) -> None:
    keeper, copy = google(cat, "IMG_0003.JPG"), google(cat, "IMG_0003_2.jpg")
    duplicate(cat, keeper, copy)
    cat.record_import(keeper, "UUID-NEW", "e" * 64, "batch-000001")

    assert line(cat, "IMG_0003_2.jpg").detail == "UUID-NEW"


def test_a_duplicate_of_a_keeper_not_imported_yet_is_unaccounted(cat: Catalogue) -> None:
    keeper, copy = google(cat, "IMG_0004.JPG"), google(cat, "IMG_0004_2.jpg")
    duplicate(cat, keeper, copy)

    found = line(cat, "IMG_0004_2.jpg")
    assert (found.status, found.detail) == ("unaccounted", "its keeper is not imported yet")


def test_a_rejected_item_gives_the_reason(cat: Catalogue) -> None:
    item = google(cat, "IMG_0005.JPG")
    cat.set_decision("item", item, "clutter", {"reject": True})

    assert (line(cat, "IMG_0005.JPG").status, line(cat, "IMG_0005.JPG").detail) == (
        "rejected-by-user", "clutter",
    )  # fmt: skip


def test_items_waiting_for_review_or_import_are_unaccounted(cat: Catalogue) -> None:
    google(cat, "IMG_0006.JPG")
    copy = google(cat, "IMG_0007.JPG")
    duplicate(cat, icloud(cat, "UUID-7"), copy, answer=False)

    assert line(cat, "IMG_0006.JPG").detail == "not imported yet"
    assert line(cat, "IMG_0007.JPG").detail == "waiting for review: duplicates"
    assert {entry.path for entry in reconcile(cat).unaccounted} == {
        f"{ROOT}/IMG_0006.JPG", f"{ROOT}/IMG_0007.JPG",
    }  # fmt: skip


def test_a_live_photos_clip_is_accounted_with_its_still(cat: Catalogue) -> None:
    still = google(cat, "IMG_0008.HEIC")
    clip = google(cat, "IMG_0008.MOV", flags=frozenset({"live-photo-motion"}))
    group = cat.upsert_group("live-pair", f"{ROOT}/IMG_0008.HEIC", {})
    cat.set_group_members(group, [(still, "still", 0), (clip, "motion", 1)])
    for member in (still, clip):
        cat.record_import(member, "UUID-LIVE", "d" * 64, "batch-000001")

    assert line(cat, "IMG_0008.MOV").detail == "UUID-LIVE"


def test_a_takeout_file_ingest_never_catalogued_is_unaccounted(cat: Catalogue) -> None:
    run = cat.start_run("ingest", None)
    lost = f"{ROOT}/lost.jpg"
    cat.log_action(run, 1, key=f"ingest:{lost}#0", kind="ingest", target=lost, details="{}")
    cat.set_action_status(run, 1, "applied")

    found = line(cat, "lost.jpg")
    assert (found.status, found.detail) == ("unaccounted", "not catalogued")


def test_album_copies_are_counted_with_their_item(cat: Catalogue) -> None:
    item = google(cat, "IMG_0009.JPG")
    cat.add_copy("google", "Takeout/Google Photos/Holiday/IMG_0009.JPG", item)
    cat.record_import(item, "UUID-9", "c" * 64, "batch-000001")
    run = cat.start_run("ingest", None)
    for seq, path in enumerate(
        [f"{ROOT}/IMG_0009.JPG", "Takeout/Google Photos/Holiday/IMG_0009.JPG"], 1
    ):
        cat.log_action(run, seq, key=f"ingest:{path}#0", kind="ingest", target=path, details="{}")
        cat.set_action_status(run, seq, "applied")

    report = reconcile(cat)

    assert [entry.path for entry in report.lines] == [f"{ROOT}/IMG_0009.JPG"]
    assert report.lines[0].copies == 1
    assert report.unaccounted == []


def test_totals_fixes_and_staging_are_counted(cat: Catalogue) -> None:
    a = google(cat, "IMG_0010.JPG", size=300)
    b = google(cat, "IMG_0011.JPG", size=200)
    cat.record_import(a, "UUID-10", "b" * 64, "batch-000001")
    cat.set_decision("item", b, "quality", {"reject": True})
    old = icloud(cat, "UUID-OLD", size=5000)
    cat.set_staged(old, "quality")
    cat.set_date_candidate(a, "user", datetime(2004, 7, 1, 12, 0))
    cat.set_date_candidate(old, "user", datetime(2004, 7, 1, 12, 0))
    cat.set_inferred_location(a, (1.0, 2.0), "neighbour", {})
    cat.set_decision("item", a, "location", {"approve": True})
    cat.set_inferred_location(b, (1.0, 2.0), "neighbour", {})

    report = reconcile(cat)

    assert report.totals()["google"] == {
        "imported": (1, 300), "rejected-by-user": (1, 200), "all": (2, 500),
    }  # fmt: skip
    assert report.totals()["icloud"] == {"all": (1, 5000), "staged for To Delete": (1, 5000)}
    assert report.reasons() == {"quality": 1}
    assert (report.date_fixes, report.location_fixes, report.staged) == (2, 1, 1)


def test_the_csv_has_one_row_per_google_item_with_the_trail(cat: Catalogue, tmp_path: Path) -> None:
    item = google(cat, "IMG_0012.JPG")
    cat.record_import(item, "UUID-12", "a" * 64, "batch-000001")
    google(cat, "IMG_0013.JPG")

    write_csv(reconcile(cat), tmp_path / "report.csv")

    with (tmp_path / "report.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["status"] for row in rows] == ["imported", "unaccounted"]
    assert list(rows[0]) == ["path", "kind", "size", "status", "detail", "copies", "sha256",
                             "working_sha256", "photos_uuid"]  # fmt: skip
    assert rows[0]["working_sha256"] == "a" * 64


def test_the_html_shows_the_totals_and_every_gap(cat: Catalogue, tmp_path: Path) -> None:
    google(cat, "IMG_0014.JPG")

    write_html(reconcile(cat), tmp_path / "report.html")

    page = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "1 unaccounted" in page
    assert "IMG_0014.JPG" in page
    assert "not imported yet" in page
