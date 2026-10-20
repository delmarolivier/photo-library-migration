"""A stand-in for Vision text detection in tests: the same text for every picture."""

from __future__ import annotations

from PIL import Image

from photomigrate.text_regions import TextSummary

SOME_TEXT = TextSummary(regions=12, coverage=0.2, tallest=0.03, banded=0.1)


class FixedText:
    def __init__(self, summary: TextSummary = SOME_TEXT) -> None:
        self.summary = summary

    def read(self, image: Image.Image) -> TextSummary:
        return self.summary
