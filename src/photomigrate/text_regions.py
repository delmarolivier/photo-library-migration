"""Text in a picture, for clutter detection (CLA-86): Apple Vision on macOS, nothing elsewhere.

Ingest and index detect text once, from the picture already decoded for hashing, with
Vision's ``VNDetectTextRectanglesRequest`` (it finds text without reading it), and store a
summary as JSON (``items.text``; ``NULL`` means not measured):

* ``regions``: how many text regions;
* ``coverage``: the share of the picture they cover (capped at 1);
* ``tallest``: the tallest region, as a share of the picture's height (big meme captions);
* ``banded``: the share of regions in the top or bottom 30% (where meme captions sit).

``text_reader()`` is ``None`` where Vision is unavailable; clutter detection then falls back
to open_clip zero-shot labels.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Protocol

from PIL import Image

from photomigrate.faces import Box, box_of, vision_data

_BAND = 0.3


@dataclass(frozen=True)
class TextSummary:
    regions: int
    coverage: float
    tallest: float
    banded: float


class TextReader(Protocol):
    def read(self, image: Image.Image) -> TextSummary | None:
        """The text in the picture, or ``None`` if it could not be read."""
        ...


def summarise(boxes: Sequence[Box]) -> TextSummary:
    """Vision's text boxes (fractions of the picture, origin bottom-left) in four numbers."""
    if not boxes:
        return TextSummary(regions=0, coverage=0.0, tallest=0.0, banded=0.0)
    banded = sum(1 for _, y, _, h in boxes if y + h / 2 < _BAND or y + h / 2 > 1 - _BAND)
    return TextSummary(
        regions=len(boxes),
        coverage=round(min(sum(w * h for _, _, w, h in boxes), 1.0), 4),
        tallest=round(max(h for _, _, _, h in boxes), 4),
        banded=round(banded / len(boxes), 4),
    )


def text_to_json(summary: TextSummary | None) -> str | None:
    return None if summary is None else json.dumps(asdict(summary), sort_keys=True)


def text_from_json(text: str | None) -> TextSummary | None:
    return None if text is None else TextSummary(**json.loads(text))


class VisionText:
    """Text regions by Apple Vision (macOS only)."""

    def read(self, image: Image.Image) -> TextSummary | None:
        import Vision

        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(vision_data(image), {})
        request = Vision.VNDetectTextRectanglesRequest.alloc().init()
        done, _ = handler.performRequests_error_([request], None)
        if not done:  # one odd picture must not stop ingest: it is left unmeasured
            return None
        return summarise([box_of(region.boundingBox()) for region in request.results() or ()])


def text_reader() -> TextReader | None:
    """Vision on macOS, or ``None`` where it is unavailable."""
    if sys.platform != "darwin":
        return None
    try:
        import Vision  # noqa: F401
    except ImportError:
        return None
    return VisionText()
