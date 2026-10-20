"""The date audit and fixes over a real catalogue: flag, batch, suggest, then apply answers."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from photomigrate.analyse.date_audit import DateAuditStage
from photomigrate.analyse.date_fixes import DateFixStage, planned_write
from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from photomigrate.ingest.stage import IngestStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.takeout import TakeoutBuilder

FOLDER = "Photos from 2019"
ROOT = f"Takeout/Google Photos/{FOLDER}"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ZONE = ZoneInfo("Europe/Paris")


def july(day: int, hour: int = 12) -> datetime:
    return datetime(2019, 7, day, hour, 0, tzinfo=UTC)


@pytest.fixture
def catalogue_path(tmp_path: Path) -> Path:
    b = TakeoutBuilder(seed=5)
    for n in (1, 2, 4, 5):
        b.photo(FOLDER, f"IMG_{n:04d}.JPG", taken=july(n))
    # a wrong camera clock: the file says 2015, Google (edited) says July 3rd
    b.photo(FOLDER, "IMG_0003.JPG", taken=july(3), exif_taken=datetime(2015, 1, 3, 14, 0))
    b.photo(FOLDER, "IMG_0006.JPG", taken=july(6), exif_taken=None, sidecar=None)
    b.photo(FOLDER, "IMG_0007.JPG", taken=july(7), exif_taken=datetime(1970, 1, 1))
    b.photo(FOLDER, "IMG_0008.JPG", taken=datetime(2031, 1, 1, tzinfo=UTC))
    b.photo(FOLDER, "IMG_0009.JPG", taken=july(9), created=july(9), exif_taken=None)
    b.photo(FOLDER, "IMG_0010.JPG", taken=july(10))
    b.photo(
        FOLDER, "IMG-20190702-WA0003.jpg", taken=july(20), created=july(20), camera=None,
        exif_taken=None,
    )  # fmt: skip
    b.write_zips(tmp_path / "takeout")
    path = tmp_path / "catalogue.sqlite"
    with IngestStage(tmp_path / "takeout", tmp_path / "work", zone=ZoneInfo("UTC")) as stage:
        run_stage(stage, catalogue_path=path, plan_dir=tmp_path / "plans", apply=True)
    with Catalogue.open(path) as catalogue:
        uuid = catalogue.upsert_item(
            CatalogueItem(source="icloud", source_key="UUID-1", kind="photo", title="IMG_7001.HEIC")
        )
        catalogue.set_date_candidate(uuid, "icloud", datetime(2000, 1, 1, 0, 0, tzinfo=UTC))
    return path


def analyse(path: Path, tmp_path: Path, *, apply: bool = True) -> tuple[RunResult, RunResult]:
    common = {"catalogue_path": path, "plan_dir": tmp_path / "plans", "apply": apply}
    fixed = run_stage(DateFixStage(), **common)  # type: ignore[arg-type]
    audited = run_stage(DateAuditStage(now=NOW), **common)  # type: ignore[arg-type]
    return fixed, audited


def short(item: CatalogueItem) -> str:
    """A Google item's file name, an iCloud item's title."""
    if item.source == "google":
        return item.source_key.removeprefix(ROOT + "/")
    return item.title or item.source_key


def batch_reasons(path: Path) -> dict[str, dict[str, str | None]]:
    """Each batch's members ({name: reasons}), by the batch's first member."""
    with Catalogue.open(path) as catalogue:
        found = {}
        for group in catalogue.groups("date-batch"):
            members = {}
            for item_id, role, _ in catalogue.group_members(group.id):
                members[short(catalogue.item(item_id))] = role
            found[next(iter(members))] = members
        return found


def batch_of(path: Path, name: str) -> tuple[int, dict[str, object]]:
    with Catalogue.open(path) as catalogue:
        for group in catalogue.groups("date-batch"):
            names = [short(catalogue.item(i)) for i, _, _ in catalogue.group_members(group.id)]
            if name in names:
                return group.id, group.details
    raise AssertionError(f"{name} is in no batch")


def answer(path: Path, name: str, value: dict[str, object]) -> None:
    group_id, _ = batch_of(path, name)
    with Catalogue.open(path) as catalogue:
        catalogue.set_decision("group", group_id, "date", value)


def google(path: Path, name: str) -> tuple[CatalogueItem, dict[str, datetime], set[str]]:
    with Catalogue.open(path) as catalogue:
        item = catalogue.get_item("google", f"{ROOT}/{name}")
        assert item is not None
        assert item.id is not None
        return item, catalogue.date_candidates(item.id), catalogue.keywords(item.id)


def test_each_doubtful_date_is_flagged_with_its_reasons_in_batches(
    catalogue_path: Path, tmp_path: Path
) -> None:
    analyse(catalogue_path, tmp_path)

    found = batch_reasons(catalogue_path)
    assert found == {
        "IMG_0003.JPG": {
            "IMG_0003.JPG": "conflict,out-of-sequence",
            "IMG_0007.JPG": "placeholder",
            "IMG_0008.JPG": "future,out-of-sequence",
        },
        "IMG_0006.JPG": {"IMG_0006.JPG": "missing", "IMG_0009.JPG": "missing,upload-as-taken"},
        "IMG-20190702-WA0003.jpg": {"IMG-20190702-WA0003.jpg": "missing,upload-as-taken"},
        "IMG_7001.HEIC": {"IMG_7001.HEIC": "placeholder"},
    }


def test_batches_carry_dates_and_suggestions_for_the_review(
    catalogue_path: Path, tmp_path: Path
) -> None:
    analyse(catalogue_path, tmp_path)

    _, undated = batch_of(catalogue_path, "IMG_0006.JPG")
    assert (undated["problem"], undated["camera"], undated["folder"], undated["prefix"]) == (
        "undated", "Apple iPhone 8", FOLDER, "IMG_",
    )  # fmt: skip
    suggested = undated["suggestions"]
    assert isinstance(suggested, dict)
    assert suggested[f"google:{ROOT}/IMG_0006.JPG"] == "2019-07-06T12:00:00", (
        "between IMG_0005 (5th, noon) and IMG_0010 (10th, noon): a fifth of the way"
    )
    _, whatsapp = batch_of(catalogue_path, "IMG-20190702-WA0003.jpg")
    assert whatsapp["suggestions"] == {
        f"google:{ROOT}/IMG-20190702-WA0003.jpg": "2019-07-02T00:00:00"
    }
    _, doubtful = batch_of(catalogue_path, "IMG_0003.JPG")
    assert doubtful["current"][f"google:{ROOT}/IMG_0003.JPG"] == "2015-01-03T14:00:00"  # type: ignore[index]


def test_a_year_answer_plans_the_first_of_july_and_the_approximate_keyword(
    catalogue_path: Path, tmp_path: Path
) -> None:
    analyse(catalogue_path, tmp_path)
    answer(catalogue_path, "IMG_0006.JPG", {"answer": "year", "year": 2004})

    fixed, _ = analyse(catalogue_path, tmp_path)

    assert fixed.plan.summary() == {"fix-date": 2}
    for name in ("IMG_0006.JPG", "IMG_0009.JPG"):
        item, candidates, keywords = google(catalogue_path, name)
        assert candidates["user"] == datetime(2004, 7, 1, 12, 0)
        assert keywords == {"date-approx"}
        written = planned_write(item, candidates, ZONE).taken
        assert written == datetime(2004, 7, 1, 12, 0, tzinfo=ZONE), "what import writes"
    assert "IMG_0006.JPG" not in str(batch_reasons(catalogue_path)), "answered: no longer asked"


def test_a_shift_answer_moves_the_whole_batch_exactly(catalogue_path: Path, tmp_path: Path) -> None:
    analyse(catalogue_path, tmp_path)
    offset = (datetime(2019, 7, 3, 14, 0) - datetime(2015, 1, 3, 14, 0)).total_seconds()
    answer(catalogue_path, "IMG_0003.JPG", {"answer": "shift", "offset_seconds": int(offset)})

    analyse(catalogue_path, tmp_path)

    item, candidates, keywords = google(catalogue_path, "IMG_0003.JPG")
    assert candidates["user"] == datetime(2019, 7, 3, 14, 0)
    assert keywords == set()
    assert planned_write(item, candidates, ZONE).taken == datetime(2019, 7, 3, 14, 0, tzinfo=ZONE)
    _, future, _ = google(catalogue_path, "IMG_0008.JPG")
    assert future["user"] == datetime(2031, 1, 1, 0, 0, tzinfo=UTC) + timedelta(seconds=offset)


def test_accepting_suggestions_fixes_the_items_that_have_one(
    catalogue_path: Path, tmp_path: Path
) -> None:
    analyse(catalogue_path, tmp_path)
    answer(catalogue_path, "IMG-20190702-WA0003.jpg", {"answer": "accept"})

    analyse(catalogue_path, tmp_path)

    item, candidates, _ = google(catalogue_path, "IMG-20190702-WA0003.jpg")
    assert candidates["user"] == datetime(2019, 7, 2)
    assert planned_write(item, candidates, ZONE).taken == datetime(2019, 7, 2, tzinfo=ZONE)
    assert json.loads(item.metadata_plan)["taken"] is None, "ingest's own plan is kept as it was"


def test_icloud_items_get_a_timewarp_planned_for_the_import(
    catalogue_path: Path, tmp_path: Path
) -> None:
    analyse(catalogue_path, tmp_path)
    answer(catalogue_path, "IMG_7001.HEIC", {"answer": "date", "date": "2012-05-06T07:08"})

    fixed, _ = analyse(catalogue_path, tmp_path)

    (action,) = fixed.plan.actions
    assert (action.kind, action.target) == ("plan-timewarp", "icloud:UUID-1")
    assert action.details["from"] == "2000-01-01T00:00:00+00:00"
    with Catalogue.open(catalogue_path) as catalogue:
        item = catalogue.get_item("icloud", "UUID-1")
        assert item is not None
        assert item.id is not None
        assert catalogue.date_candidates(item.id)["user"] == datetime(2012, 5, 6, 7, 8)
        assert item.metadata_plan == "{}", "Photos items are changed by timewarp, not a copy"


def test_answers_apply_once_and_re_running_changes_nothing(
    catalogue_path: Path, tmp_path: Path
) -> None:
    analyse(catalogue_path, tmp_path)
    answer(catalogue_path, "IMG_0003.JPG", {"answer": "shift", "offset_seconds": 3600})
    analyse(catalogue_path, tmp_path)
    before = google(catalogue_path, "IMG_0003.JPG")[1]["user"]

    fixed, audited = analyse(catalogue_path, tmp_path)

    assert (fixed.plan.actions, audited.plan.actions) == ((), ())
    assert google(catalogue_path, "IMG_0003.JPG")[1]["user"] == before, "never shifted twice"


def test_a_shift_is_never_applied_twice_even_before_the_audit_runs(
    catalogue_path: Path, tmp_path: Path
) -> None:
    analyse(catalogue_path, tmp_path)
    answer(catalogue_path, "IMG_0003.JPG", {"answer": "shift", "offset_seconds": 3600})
    common = {"catalogue_path": catalogue_path, "plan_dir": tmp_path / "plans", "apply": True}
    run_stage(DateFixStage(), **common)  # type: ignore[arg-type]

    again = run_stage(DateFixStage(), **common)  # type: ignore[arg-type]

    assert again.plan.actions == ()
    assert google(catalogue_path, "IMG_0003.JPG")[1]["user"] == datetime(2015, 1, 3, 15, 0)


def test_a_bad_answer_is_reported_and_the_others_still_apply(
    catalogue_path: Path, tmp_path: Path
) -> None:
    analyse(catalogue_path, tmp_path)
    answer(catalogue_path, "IMG_0006.JPG", {"answer": "year", "year": "soon"})
    answer(catalogue_path, "IMG_7001.HEIC", {"answer": "keep"})

    fixed, _ = analyse(catalogue_path, tmp_path)

    assert fixed.plan.summary() == {"bad-answer": 1, "plan-timewarp": 1}
    bad = next(a for a in fixed.plan.actions if a.kind == "bad-answer")
    assert "a whole year" in str(bad.details["error"])
    assert "IMG_0006.JPG" in str(batch_reasons(catalogue_path))


def test_a_dry_run_stores_nothing(catalogue_path: Path, tmp_path: Path) -> None:
    _, audited = analyse(catalogue_path, tmp_path, apply=False)

    assert audited.plan.summary() == {"propose-date-batch": 4}
    assert batch_reasons(catalogue_path) == {}


def test_edits_creations_duplicates_and_motion_clips_are_not_asked_about(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=6)
    original = b.photo(FOLDER, "IMG_0001.JPG", taken=july(1), exif_taken=None, sidecar=None)
    b.edited(original)
    b.google_creation(b.photo(FOLDER, "IMG_0002.JPG", taken=july(2)), "MOTION")
    b.live_photo(FOLDER, "IMG_0003", taken=july(3))
    b.write_zips(tmp_path / "takeout")
    path = tmp_path / "catalogue.sqlite"
    with IngestStage(tmp_path / "takeout", tmp_path / "work", zone=ZoneInfo("UTC")) as stage:
        run_stage(stage, catalogue_path=path, plan_dir=tmp_path / "plans", apply=True)
    from photomigrate.ingest.livephotos import LivePhotoStage

    run_stage(LivePhotoStage(), catalogue_path=path, plan_dir=tmp_path / "plans", apply=True)
    with Catalogue.open(path) as catalogue:
        keeper = catalogue.get_item("google", f"{ROOT}/IMG_0002.JPG")
        assert keeper is not None
        copy = catalogue.upsert_item(
            CatalogueItem(
                source="icloud", source_key="UUID-DUP", kind="photo", title="IMG_0002.JPG"
            )
        )
        group = catalogue.upsert_group("duplicate", "dup-x", {"keeper": "x", "method": "exact"})
        catalogue.set_group_members(group, [(keeper.id or 0, "keeper", 0), (copy, "duplicate", 1)])

    analyse(path, tmp_path)

    asked = {name for batch in batch_reasons(path).values() for name in batch}
    assert asked == {"IMG_0001.JPG"}


def test_proposals_never_decide(catalogue_path: Path, tmp_path: Path) -> None:
    analyse(catalogue_path, tmp_path)

    with sqlite3.connect(catalogue_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone() == (0,)


def test_analyse_audits_dates_and_applies_answers(catalogue_path: Path, tmp_path: Path) -> None:
    args = ["--catalogue", str(catalogue_path), "--plan-dir", str(tmp_path / "plans")]
    runner = CliRunner()

    first = runner.invoke(app, [*args, "analyse", "--apply"])

    assert first.exit_code == 0, first.output
    assert "date-audit" in first.output
    answer(catalogue_path, "IMG_0006.JPG", {"answer": "date", "date": "2019-07-06T10:00"})
    again = runner.invoke(app, [*args, "analyse", "--apply"])
    assert again.exit_code == 0, again.output
    assert "date-fixes" in again.output
    assert google(catalogue_path, "IMG_0006.JPG")[1]["user"] == datetime(2019, 7, 6, 10, 0)


def test_actions_for_items_that_are_gone_are_errors(tmp_path: Path) -> None:
    from photomigrate.runner import Action

    batch = Action("propose-date-batch", "dates-x", {"members": [["icloud", "GONE", "missing"]]})
    fix = Action("fix-date", "google:GONE", {"source": "google", "key": "GONE", "to": "2019-01-01"})
    with Catalogue.snapshot(tmp_path / "none") as catalogue:
        with pytest.raises(KeyError, match="GONE"):
            DateAuditStage(now=NOW).apply(batch, catalogue)
        with pytest.raises(KeyError, match="GONE"):
            DateFixStage().apply(fix, catalogue)
