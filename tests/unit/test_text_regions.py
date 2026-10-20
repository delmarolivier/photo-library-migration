"""Text regions in a picture, for clutter: a summary of Vision's text boxes, or nothing."""

import builtins
import sys
from typing import Any

import pytest

from photomigrate import text_regions
from photomigrate.text_regions import TextSummary, summarise, text_from_json, text_to_json


def test_no_boxes_is_no_text() -> None:
    assert summarise([]) == TextSummary(regions=0, coverage=0.0, tallest=0.0, banded=0.0)


def test_a_summary_counts_regions_their_area_their_height_and_where_they_sit() -> None:
    top = (0.1, 0.85, 0.8, 0.1)  # origin bottom-left: this one is near the top
    bottom = (0.1, 0.02, 0.8, 0.12)
    middle = (0.4, 0.45, 0.2, 0.05)

    found = summarise([top, bottom, middle])

    assert found.regions == 3
    assert found.coverage == pytest.approx(0.08 + 0.096 + 0.01, abs=1e-4)
    assert found.tallest == pytest.approx(0.12)
    assert found.banded == pytest.approx(2 / 3, abs=1e-3)


def test_coverage_never_exceeds_the_whole_picture() -> None:
    assert summarise([(0, 0, 1, 1), (0, 0, 1, 1)]).coverage == 1.0


def test_summaries_round_trip_through_json_and_none_means_not_measured() -> None:
    found = summarise([(0.1, 0.85, 0.8, 0.1)])

    assert text_from_json(text_to_json(found)) == found
    assert text_to_json(None) is None
    assert text_from_json(None) is None


def test_there_is_no_text_reader_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    assert text_regions.text_reader() is None


def test_there_is_no_text_reader_without_pyobjc_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def without_vision(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "Vision":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(builtins, "__import__", without_vision)

    assert text_regions.text_reader() is None
