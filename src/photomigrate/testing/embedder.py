"""A stand-in for open_clip in tests: no model, no download, and deterministic.

The vector is the picture's 16x16 greyscale thumbnail, centred on its mean and scaled to unit
length. So the same scene a moment later, blurred or darker comes out close, while different
seeded scenes come out far apart. That is enough to test grouping, not to judge real photos.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from photomigrate.embedding import Vector

_SIDE = 16


class ThumbnailEmbedder:
    def embed(self, image: Image.Image) -> Vector:
        small = image.convert("L").resize((_SIDE, _SIDE), Image.Resampling.BOX)
        pixels = np.asarray(small, dtype=np.float32).ravel()
        centred = pixels - pixels.mean()
        norm = float(np.linalg.norm(centred))
        return centred / norm if norm else centred
