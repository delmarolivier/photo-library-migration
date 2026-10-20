"""Image embeddings for best-shot grouping (CLA-84): open_clip vectors, compared by cosine.

Ingest and index embed every photo once, from the picture already decoded for its hashes and
metrics (``hashing.open_picture``), and store the vector as half-precision bytes
(``items.embedding``, 1 KB per photo). ``analyse`` only compares stored vectors.

The model is ``ViT-B-32`` with LAION-2B weights. open_clip downloads them (about 600 MB, into
the Hugging Face cache) the first time a photo is embedded, and they run on Apple silicon
(MPS) when there is one. Vectors from different models cannot be compared, so changing the
model means measuring every photo again.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray
from PIL import Image

type Vector = NDArray[np.float32]

DEFAULT_MODEL = "ViT-B-32"
DEFAULT_PRETRAINED = "laion2b_s34b_b79k"


class Embedder(Protocol):
    def embed(self, image: Image.Image) -> Vector:
        """A unit-length vector for the picture."""
        ...

    def embed_texts(self, texts: Sequence[str]) -> list[Vector]:
        """Unit-length vectors for text prompts, in the pictures' space (zero-shot labels)."""
        ...


def _best_device() -> str:
    import torch

    return "mps" if torch.backends.mps.is_available() else "cpu"


class ClipEmbedder:
    """open_clip's image encoder. The model loads on first use, so building one is cheap.

    ``pretrained=None`` means random weights: no download (tests use that).
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        pretrained: str | None = DEFAULT_PRETRAINED,
        device: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.pretrained = pretrained
        self.device = device or _best_device()
        self._model: Any = None
        self._preprocess: Any = None
        self._tokenizer: Any = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> tuple[Any, Any]:
        if self._model is None:
            import open_clip

            model, _, preprocess = open_clip.create_model_and_transforms(
                self.model_name, pretrained=self.pretrained, device=self.device
            )
            self._model, self._preprocess = model.eval(), preprocess
            self._tokenizer = open_clip.get_tokenizer(self.model_name)
        return self._model, self._preprocess

    def embed_texts(self, texts: Sequence[str]) -> list[Vector]:
        import torch

        model, _ = self._load()
        with torch.no_grad():
            tokens = self._tokenizer(list(texts)).to(self.device)
            vectors = model.encode_text(tokens).float().cpu().numpy()
        return [np.asarray(v / np.linalg.norm(v), dtype=np.float32) for v in vectors]

    def embed(self, image: Image.Image) -> Vector:
        import torch

        model, preprocess = self._load()
        with torch.no_grad():
            batch = preprocess(image.convert("RGB")).unsqueeze(0).to(self.device)
            vector = model.encode_image(batch).float().cpu().numpy()[0]
        return np.asarray(vector / np.linalg.norm(vector), dtype=np.float32)


def default_embedder() -> Embedder:
    """What ingest and index use: open_clip with its pretrained weights."""
    return ClipEmbedder()


def to_bytes(vector: Vector) -> bytes:
    """A vector as stored in the catalogue: half precision, 2 bytes per value."""
    return np.asarray(vector, dtype=np.float16).tobytes()


def from_bytes(data: bytes) -> Vector:
    return np.frombuffer(data, dtype=np.float16).astype(np.float32)


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity of two vectors: 1 for the same direction, 0 at right angles."""
    if a.shape != b.shape:
        raise ValueError("vectors of different lengths come from different models")
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
