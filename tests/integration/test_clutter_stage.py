"""The clutter stage over a real catalogue: each category from fixtures, nothing decided."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from typer.testing import CliRunner

from photomigrate.analyse.clutter import ClutterStage, ZeroShot
from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from photomigrate.config import ClutterConfig
from photomigrate.embedding import Vector
from photomigrate.ingest.stage import IngestStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.embedder import ThumbnailEmbedder
from photomigrate.testing.media import captioned, synthetic_photo, text_page, with_sign
from photomigrate.testing.takeout import TakeoutBuilder
from photomigrate.text_regions import TextSummary, text_to_json

FOLDER = "Photos from 2019"
ROOT = f"Takeout/Google Photos/{FOLDER}"
WHEN = datetime(2019, 7, 1, 12, 0, tzinfo=UTC)
RECEIPT = ["SUPERMARKET", "Milk 1.20", "Bread 2.10", "Eggs 3.40", "Apples 1.99", "Cheese 4.50",
           "Coffee 5.75", "TOTAL 18.94", "Thank you", "VISA ****1234"]  # fmt: skip
# What Vision finds in these fixtures (tests/macos/test_clutter.py checks it for real)
TEXT = {
    "received_1234567.jpeg": TextSummary(regions=2, coverage=0.1, tallest=0.13, banded=1.0),
    "scan0001.jpg": TextSummary(regions=10, coverage=0.06, tallest=0.025, banded=0.2),
    "IMG_3001.JPG": TextSummary(regions=1, coverage=0.005, tallest=0.045, banded=0.0),
}
EXPECTED = {
    "Screenshot_20191105-123456.png": "screenshot",
    "Screenshot_20191106-101010.png": "screenshot",  # its edit is not queued twice
    "IMG-20190702-WA0003.jpg": "forward",
    "received_1234567.jpeg": "meme",
    "scan0001.jpg": "document",
    "IMG_2001-MOTION.gif": "google-creation",
    "IMG_0042.PNG": "screenshot",
}


def build(tmp_path: Path) -> Path:
    b = TakeoutBuilder(seed=9)
    stripped = {"camera": None, "exif_taken": None}
    b.photo(FOLDER, "Screenshot_20191105-123456.png", taken=WHEN, size=(750, 1334), **stripped)  # type: ignore[arg-type]
    b.photo(FOLDER, "IMG-20190702-WA0003.jpg", taken=WHEN, size=(1600, 1200), **stripped)  # type: ignore[arg-type]
    meme = captioned(synthetic_photo(3, (800, 600)), top="WHEN THE CODE", bottom="WORKS")
    b.photo(FOLDER, "received_1234567.jpeg", taken=WHEN, image=meme, **stripped)  # type: ignore[arg-type]
    b.photo(FOLDER, "scan0001.jpg", taken=WHEN, image=text_page(RECEIPT), **stripped)  # type: ignore[arg-type]
    b.photo(FOLDER, "IMG_3001.JPG", taken=WHEN, image=with_sign(synthetic_photo(4), "STOP"))
    b.photo(FOLDER, "IMG_3002.JPG", taken=WHEN, image=synthetic_photo(5))
    b.google_creation(b.photo(FOLDER, "IMG_2001.JPG", taken=WHEN), "MOTION")
    b.edited(b.photo(FOLDER, "Screenshot_20191106-101010.png", taken=WHEN, **stripped))  # type: ignore[arg-type]
    b.write_zips(tmp_path / "takeout")
    return tmp_path / "takeout"


def ingest(takeout: Path, tmp_path: Path, **readers: object) -> Path:
    path = tmp_path / "catalogue.sqlite"
    with IngestStage(takeout, tmp_path / "work", zone=ZoneInfo("UTC"), **readers) as stage:  # type: ignore[arg-type]
        run_stage(stage, catalogue_path=path, plan_dir=tmp_path / "plans", apply=True)
    with Catalogue.open(path) as catalogue:
        catalogue.upsert_item(
            CatalogueItem(
                source="icloud", source_key="UUID-SHOT", kind="photo", title="IMG_0042.PNG",
                flags=frozenset({"screenshot"}), camera="Apple iPhone 12",
            )
        )  # fmt: skip
    return path


@pytest.fixture
def catalogue_path(tmp_path: Path) -> Path:
    """The fixtures, with Vision's text as it would be measured on macOS."""
    path = ingest(build(tmp_path), tmp_path)
    with Catalogue.open(path) as catalogue:
        for name, summary in TEXT.items():
            item = catalogue.get_item("google", f"{ROOT}/{name}")
            assert item is not None
            catalogue.upsert_item(item.replace(text=text_to_json(summary)))
        for item in catalogue.items("google"):
            if item.kind == "photo" and item.text is None:
                empty = TextSummary(regions=0, coverage=0.0, tallest=0.0, banded=0.0)
                catalogue.upsert_item(item.replace(text=text_to_json(empty)))
    return path


def clutter(
    path: Path, tmp_path: Path, config: ClutterConfig | None = None, *, apply: bool = True,
    zero_shot: object = None,
) -> RunResult:  # fmt: skip
    return run_stage(
        ClutterStage(config or ClutterConfig(), zero_shot),  # type: ignore[arg-type]
        catalogue_path=path,
        plan_dir=tmp_path / "plans",
        apply=apply,
    )


def queue(path: Path) -> dict[str, str]:
    with Catalogue.open(path) as catalogue:
        found = {}
        for item_id, (category, _, _) in catalogue.clutter().items():
            item = catalogue.item(item_id)
            name = (
                item.source_key.removeprefix(ROOT + "/") if item.source == "google" else item.title
            )
            found[name or ""] = category
        return found


def test_each_category_is_queued_and_photos_stay_out(catalogue_path: Path, tmp_path: Path) -> None:
    clutter(catalogue_path, tmp_path)

    assert queue(catalogue_path) == EXPECTED


def test_queued_items_carry_a_score_and_their_evidence(
    catalogue_path: Path, tmp_path: Path
) -> None:
    clutter(catalogue_path, tmp_path)

    with Catalogue.open(catalogue_path) as catalogue:
        found = list(catalogue.clutter().values())
    assert all(0.5 <= score <= 1 for _, score, _ in found)
    assert all(evidence for _, _, evidence in found)


def test_re_running_changes_nothing_and_a_dry_run_stores_nothing(
    catalogue_path: Path, tmp_path: Path
) -> None:
    dry = clutter(catalogue_path, tmp_path, apply=False)
    assert dry.plan.summary() == {"set-clutter": len(EXPECTED)}
    assert queue(catalogue_path) == {}

    clutter(catalogue_path, tmp_path)
    again = clutter(catalogue_path, tmp_path)

    assert again.plan.actions == ()


def test_a_stricter_threshold_takes_the_weaker_items_out(
    catalogue_path: Path, tmp_path: Path
) -> None:
    clutter(catalogue_path, tmp_path)

    strict = clutter(catalogue_path, tmp_path, ClutterConfig(min_score=0.95))

    assert "clear-clutter" in strict.plan.summary()
    assert set(queue(catalogue_path)) < set(EXPECTED)
    assert queue(catalogue_path)["IMG_2001-MOTION.gif"] == "google-creation"


class DocumentsEverywhere:
    """A zero-shot labeller that sees a document in every picture."""

    def labels(self, vector: Vector) -> dict[str, float]:
        return {"document": 0.95, "photo": 0.05}


def test_without_vision_text_the_zero_shot_labels_are_the_fallback(tmp_path: Path) -> None:
    path = ingest(build(tmp_path), tmp_path, embedder=ThumbnailEmbedder())

    clutter(path, tmp_path, zero_shot=DocumentsEverywhere())

    found = queue(path)
    assert found["scan0001.jpg"] == "document"
    assert found["IMG_3002.JPG"] == "document", "zero-shot alone decides without Vision"


def test_real_zero_shot_labels_run_on_stored_embeddings(tmp_path: Path) -> None:
    path = ingest(build(tmp_path), tmp_path, embedder=ThumbnailEmbedder())

    result = clutter(path, tmp_path, apply=False, zero_shot=ZeroShot(ThumbnailEmbedder()))

    assert result.plan.actions


def test_proposals_never_decide(catalogue_path: Path, tmp_path: Path) -> None:
    clutter(catalogue_path, tmp_path)

    with sqlite3.connect(catalogue_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone() == (0,)


def test_an_action_for_an_item_that_is_gone_is_an_error(tmp_path: Path) -> None:
    from photomigrate.runner import Action

    action = Action("set-clutter", "GONE", {"source": "icloud", "category": "meme", "score": 1})
    with Catalogue.snapshot(tmp_path / "none") as catalogue, pytest.raises(KeyError, match="GONE"):
        ClutterStage(ClutterConfig()).apply(action, catalogue)


def test_analyse_fills_the_clutter_queue(catalogue_path: Path, tmp_path: Path) -> None:
    args = ["--catalogue", str(catalogue_path), "--plan-dir", str(tmp_path / "plans")]

    result = CliRunner().invoke(app, [*args, "analyse", "--apply"])

    assert result.exit_code == 0, result.output
    assert queue(catalogue_path) == EXPECTED


def test_zero_shot_labels_sum_to_one_on_a_stored_embedding() -> None:
    labels = ZeroShot(ThumbnailEmbedder()).labels(np.ones(256, dtype=np.float32) / 16)

    assert sum(labels.values()) == pytest.approx(1.0, abs=1e-3)
