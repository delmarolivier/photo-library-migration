"""Clutter on macOS: Vision's real text detection on the rendered fixtures."""

from pathlib import Path

import pytest

from photomigrate.analyse.clutter import ClutterStage
from photomigrate.config import ClutterConfig
from photomigrate.runner import run_stage
from photomigrate.text_regions import VisionText
from tests.integration.test_clutter_stage import EXPECTED, build, ingest, queue

pytestmark = pytest.mark.macos


def test_vision_text_puts_receipts_and_memes_in_the_queue_and_leaves_signs(tmp_path: Path) -> None:
    path = ingest(build(tmp_path), tmp_path, text=VisionText())

    run_stage(
        ClutterStage(ClutterConfig()), catalogue_path=path, plan_dir=tmp_path / "plans", apply=True
    )

    assert queue(path) == EXPECTED
