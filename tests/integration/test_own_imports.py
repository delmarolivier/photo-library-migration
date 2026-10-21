"""After an import, a re-index brings our own imports back as iCloud items. They are the same
pictures as their Google originals, so no analysis may ask about them, or stage them."""

import json
from datetime import datetime
from pathlib import Path

from photomigrate.analyse.best_shot import BestShotStage
from photomigrate.analyse.clutter import ClutterStage
from photomigrate.analyse.date_audit import DateAuditStage, dated_items
from photomigrate.analyse.dedupe_photo import DedupeStage
from photomigrate.analyse.dedupe_video import VideoDedupeStage
from photomigrate.analyse.locations import LocationStage
from photomigrate.analyse.quality import QualityStage
from photomigrate.catalogue import Catalogue
from photomigrate.config import BestShotConfig, ClutterConfig, Config, LocationConfig
from photomigrate.importer.outcomes import to_delete
from photomigrate.runner import run_stage
from tests.integration.test_import_stage import run_import, setup  # noqa: F401

NOW = datetime(2026, 10, 21)


def reindex(path: Path) -> set[int]:
    """What index would store for each imported asset: the same picture, as an iCloud item."""
    made = set()
    with Catalogue.open(path) as catalogue:
        for item_id, (uuid, working, _) in catalogue.imports().items():
            if catalogue.get_item("icloud", uuid) is not None:
                continue
            google = catalogue.item(item_id)
            asset = google.replace(
                source="icloud", source_key=uuid, sha256=working, latitude=None, longitude=None,
                original_path=f"originals/{uuid}", flags=frozenset(), id=None,
                quality=json.dumps({"sharpness": 1.0, "brightness": 5.0, "dark_fraction": 0.9,
                                    "bright_fraction": 0.0, "contrast": 1.0, "megapixels": 0.01,
                                    "short_side": 10}),
            )  # fmt: skip
            new = catalogue.upsert_item(asset)
            catalogue.set_date_candidate(new, "icloud", datetime(1970, 1, 1))
            made.add(new)
    return made


def test_a_reindex_after_an_import_asks_nothing_about_our_own_imports(  # type: ignore[no-untyped-def]
    setup,  # noqa: F811
    tmp_path: Path,
) -> None:
    run_import(setup, tmp_path)
    path = setup[0]
    ours = reindex(path)
    assert ours
    common = {"catalogue_path": path, "plan_dir": tmp_path / "plans", "apply": True}
    for stage in (
        DedupeStage(), VideoDedupeStage(), QualityStage(Config().quality),
        BestShotStage(BestShotConfig(min_similarity=-1.0)),
        ClutterStage(ClutterConfig(min_score=0.0)), LocationStage(LocationConfig()),
        DateAuditStage(now=NOW),
    ):  # fmt: skip
        run_stage(stage, **common)  # type: ignore[arg-type]

    with Catalogue.open(path) as catalogue:
        assert catalogue.own_imports() == ours
        grouped = {
            item_id
            for kind in ("duplicate", "best-shot", "date-batch")
            for group in catalogue.groups(kind)  # type: ignore[arg-type]
            for item_id, _, _ in catalogue.group_members(group.id)
        }
        assert not grouped & ours
        assert not set(catalogue.quality_flags()) & ours
        assert not set(catalogue.clutter()) & ours
        assert not set(catalogue.inferred_locations()) & ours
        assert not {d.id for d in dated_items(catalogue)} & ours
        for item_id in ours:
            catalogue.set_decision("item", item_id, "quality", {"reject": True})
        assert not set(to_delete(catalogue)) & ours, "an import is never staged for deletion"
