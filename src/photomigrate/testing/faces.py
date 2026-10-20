"""A stand-in for Apple Vision in tests: the same faces for every picture, on any platform."""

from __future__ import annotations

from collections.abc import Sequence

from PIL import Image

from photomigrate.faces import Face

ONE_FACE = (Face((0.1, 0.2, 0.3, 0.4), 0.8, eyes_open=True),)


class FixedFaces:
    """Finds ``faces`` in every picture, and records the pictures it was shown."""

    def __init__(self, faces: Sequence[Face] = ONE_FACE) -> None:
        self.faces = list(faces)
        self.seen: list[Image.Image] = []

    def read(self, image: Image.Image) -> list[Face]:
        self.seen.append(image)
        return list(self.faces)
