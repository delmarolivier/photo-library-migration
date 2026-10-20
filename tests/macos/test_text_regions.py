"""Apple Vision text detection on rendered receipts, memes and street signs."""

import pytest

from photomigrate.testing.media import captioned, synthetic_photo, text_page, with_sign
from photomigrate.text_regions import VisionText, text_reader

pytestmark = pytest.mark.macos

RECEIPT = ["SUPERMARKET", "Milk 1.20", "Bread 2.10", "Eggs 3.40", "Apples 1.99", "Cheese 4.50",
           "Coffee 5.75", "TOTAL 18.94", "Thank you", "VISA ****1234"]  # fmt: skip


def test_vision_is_the_text_reader_on_macos() -> None:
    assert isinstance(text_reader(), VisionText)


def test_a_receipt_is_many_lines_of_text() -> None:
    found = VisionText().read(text_page(RECEIPT, size=(600, 900)))

    assert found is not None
    assert found.regions >= 8


def test_a_meme_has_tall_text_at_its_edges() -> None:
    picture = captioned(synthetic_photo(3, (800, 600)), top="WHEN THE CODE", bottom="WORKS")

    found = VisionText().read(picture)

    assert found is not None
    assert found.tallest > 0.08
    assert found.banded == 1.0


def test_a_street_sign_is_a_little_text() -> None:
    found = VisionText().read(with_sign(synthetic_photo(4, (800, 600)), "STOP"))

    assert found is not None
    assert 0 < found.coverage < 0.02


def test_a_photo_without_text_has_none() -> None:
    found = VisionText().read(synthetic_photo(5))

    assert found is not None
    assert found.regions == 0


def test_a_picture_vision_cannot_read_has_no_text_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import Vision

    class Refusing:
        @classmethod
        def alloc(cls) -> "Refusing":
            return cls()

        def initWithData_options_(self, data: object, options: object) -> "Refusing":  # noqa: N802
            return self

        def performRequests_error_(self, requests: object, error: object) -> tuple[bool, None]:  # noqa: N802
            return False, None

    monkeypatch.setattr(Vision, "VNImageRequestHandler", Refusing)

    assert VisionText().read(synthetic_photo(5)) is None
