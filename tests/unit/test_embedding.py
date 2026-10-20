"""Image embeddings: open_clip vectors, stored compactly, compared by cosine similarity.

The real model's weights are never downloaded in tests: ``ClipEmbedder`` runs with random
weights (``pretrained=None``), which exercises the same code. Grouping tests use the
``ThumbnailEmbedder`` stand-in.
"""

import numpy as np
import pytest
from PIL import ImageEnhance, ImageFilter

from photomigrate.embedding import (
    DEFAULT_MODEL,
    DEFAULT_PRETRAINED,
    ClipEmbedder,
    cosine,
    default_embedder,
    from_bytes,
    to_bytes,
)
from photomigrate.testing.embedder import ThumbnailEmbedder
from photomigrate.testing.media import synthetic_image, synthetic_photo


def unit(*values: float) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def test_vectors_survive_storage_as_half_precision_bytes() -> None:
    vector = unit(*np.random.default_rng(1).normal(size=512))

    stored = to_bytes(vector)

    assert len(stored) == 512 * 2
    assert np.allclose(from_bytes(stored), vector, atol=1e-3)
    assert from_bytes(stored).dtype == np.float32


def test_cosine_similarity_is_one_for_the_same_direction_and_zero_at_right_angles() -> None:
    assert cosine(unit(1, 2, 3), unit(2, 4, 6)) == pytest.approx(1.0)
    assert cosine(unit(1, 0), unit(0, 1)) == pytest.approx(0.0)
    assert cosine(unit(1, 0), unit(-1, 0)) == pytest.approx(-1.0)


def test_vectors_of_different_models_cannot_be_compared() -> None:
    with pytest.raises(ValueError, match="different lengths"):
        cosine(unit(1, 0), unit(1, 0, 0))


def test_the_clip_embedder_loads_its_model_only_when_first_used() -> None:
    embedder = ClipEmbedder(pretrained=None, device="cpu")

    assert not embedder.loaded
    vector = embedder.embed(synthetic_image(1))

    assert embedder.loaded
    assert vector.shape == (512,)
    assert vector.dtype == np.float32
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-5)


def test_the_clip_embedder_gives_the_same_vector_for_the_same_picture() -> None:
    embedder = ClipEmbedder(pretrained=None, device="cpu")
    picture = synthetic_photo(3, size=(400, 300))

    assert np.array_equal(embedder.embed(picture), embedder.embed(picture.copy()))


def test_the_clip_embedder_takes_any_image_mode() -> None:
    embedder = ClipEmbedder(pretrained=None, device="cpu")

    vector = embedder.embed(synthetic_image(2).convert("L"))

    assert vector.shape == (512,)


def test_the_device_defaults_to_apple_silicon_when_there_is_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import torch

    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    assert ClipEmbedder(pretrained=None).device == "mps"
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    assert ClipEmbedder(pretrained=None).device == "cpu"


def test_the_default_embedder_is_open_clip_with_pretrained_weights() -> None:
    embedder = default_embedder()

    assert isinstance(embedder, ClipEmbedder)
    assert (embedder.model_name, embedder.pretrained) == (DEFAULT_MODEL, DEFAULT_PRETRAINED)
    assert not embedder.loaded, "nothing is downloaded until a photo is embedded"


def test_the_thumbnail_stand_in_puts_bursts_close_and_other_scenes_far() -> None:
    embedder = ThumbnailEmbedder()
    first = embedder.embed(synthetic_photo(5))
    burst = embedder.embed(synthetic_image(5, (800, 600), jitter=6))
    blurred = embedder.embed(synthetic_photo(5).filter(ImageFilter.GaussianBlur(4)))
    darker = embedder.embed(ImageEnhance.Brightness(synthetic_photo(5)).enhance(0.4))
    others = [embedder.embed(synthetic_photo(seed)) for seed in range(6, 12)]

    assert min(cosine(first, v) for v in (burst, blurred, darker)) > 0.95
    assert max(cosine(first, other) for other in others) < 0.8


def test_the_clip_embedder_embeds_text_prompts_in_the_image_space() -> None:
    embedder = ClipEmbedder(pretrained=None, device="cpu")

    receipt, landscape = embedder.embed_texts(["a photo of a receipt", "a landscape"])

    assert receipt.shape == (512,) == embedder.embed(synthetic_image(1)).shape
    assert float(np.linalg.norm(receipt)) == pytest.approx(1.0, abs=1e-5)
    assert not np.array_equal(receipt, landscape)
    assert np.array_equal(embedder.embed_texts(["a photo of a receipt"])[0], receipt)


def test_the_stand_in_embeds_text_deterministically_in_its_own_space() -> None:
    embedder = ThumbnailEmbedder()

    first, second = embedder.embed_texts(["a meme", "a receipt"])

    assert first.shape == embedder.embed(synthetic_image(1)).shape
    assert float(np.linalg.norm(first)) == pytest.approx(1.0, abs=1e-5)
    assert np.array_equal(ThumbnailEmbedder().embed_texts(["a meme"])[0], first)
    assert not np.array_equal(first, second)
