"""Clutter: screenshots, documents, memes, messaging forwards and Google creations."""

from dataclasses import replace

import numpy as np
import pytest

from photomigrate.analyse.clutter import CATEGORIES, Clues, ZeroShot, classify, scores
from photomigrate.text_regions import TextSummary

CAMERA = "Apple iPhone 8"
NO_TEXT = TextSummary(regions=0, coverage=0.0, tallest=0.0, banded=0.0)
RECEIPT = TextSummary(regions=10, coverage=0.06, tallest=0.025, banded=0.2)
MEME = TextSummary(regions=2, coverage=0.1, tallest=0.13, banded=1.0)
SIGN = TextSummary(regions=1, coverage=0.005, tallest=0.045, banded=0.0)


def clues(
    name: str = "IMG_1234.JPG",
    *,
    camera: str | None = CAMERA,
    dated: bool = True,
    size: tuple[int, int] = (4032, 3024),
    flags: frozenset[str] = frozenset(),
    text: TextSummary | None = NO_TEXT,
    bright: float | None = 0.05,
    labels: dict[str, float] | None = None,
    kind: str = "photo",
) -> Clues:
    return Clues(
        name=name, kind=kind, camera=camera, has_file_date=dated, width=size[0],  # type: ignore[arg-type]
        height=size[1], flags=flags, text=text, bright_fraction=bright, labels=labels,
    )  # fmt: skip


def category(found: Clues) -> str | None:
    result = classify(found, min_score=0.5)
    return None if result is None else result[0]


def test_a_camera_photo_is_not_clutter() -> None:
    assert category(clues()) is None
    assert all(score < 0.3 for score, _ in scores(clues()).values())


# --- screenshots ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Screenshot_20191105-123456.png", "Screen Shot 2019-11-05 at 1.34.56 PM.png",
     "Capture d\u2019écran 2019-11-05 à 12.34.56.png", "Bildschirmfoto 2019-11-05 um 12.34.56.png"],
)  # fmt: skip
def test_a_screenshot_by_name(name: str) -> None:
    found = clues(name, camera=None, dated=False, size=(1000, 700))

    assert category(found) == "screenshot"


def test_a_png_the_size_of_a_phone_screen_without_a_camera_is_a_screenshot() -> None:
    found = clues("IMG_0042.PNG", camera=None, dated=False, size=(1170, 2532))

    assert category(found) == "screenshot"
    assert set(scores(found)["screenshot"][1]) == {"screen-size", "png", "no-camera"}


def test_photos_knows_its_own_screenshots() -> None:
    found = clues("IMG_0042.PNG", flags=frozenset({"screenshot"}), size=(100, 100))

    assert category(found) == "screenshot"


def test_a_camera_photo_cropped_to_a_screen_size_is_not_a_screenshot() -> None:
    assert category(clues("IMG_1234.JPG", size=(1170, 2532))) is None


# --- documents -----------------------------------------------------------------------------


def test_a_receipt_on_white_paper_is_a_document() -> None:
    found = clues("IMG_2001.JPG", text=RECEIPT, bright=0.7)

    assert category(found) == "document", "photographed receipts are documents too"


def test_a_scan_is_a_document() -> None:
    page = TextSummary(regions=40, coverage=0.3, tallest=0.02, banded=0.2)

    assert category(clues("scan0001.jpg", camera=None, dated=False, text=page)) == "document"


def test_a_camera_photo_with_a_street_sign_is_not_a_document() -> None:
    found = clues("IMG_3001.JPG", text=SIGN, bright=0.1)

    assert category(found) is None
    assert scores(found)["document"][0] < 0.2


def test_missing_camera_exif_alone_never_makes_a_document() -> None:
    stripped = clues("holiday.jpg", camera=None, dated=False, text=NO_TEXT, bright=0.9)

    assert scores(stripped)["document"][0] == 0.0


def test_without_vision_text_the_zero_shot_labels_decide() -> None:
    likely = clues("IMG_2001.JPG", text=None, labels={"document": 0.8, "photo": 0.2})
    unlikely = clues("IMG_2002.JPG", text=None, labels={"document": 0.1, "photo": 0.9})
    unknown = clues("IMG_2003.JPG", text=None, labels=None)

    assert category(likely) == "document"
    assert category(unlikely) is None
    assert category(unknown) is None


def test_zero_shot_labels_are_not_used_when_vision_measured_text() -> None:
    measured = clues("IMG_2001.JPG", text=NO_TEXT, labels={"document": 0.9})

    assert category(measured) is None


# --- memes ---------------------------------------------------------------------------------


def test_big_captions_on_a_saved_picture_are_a_meme() -> None:
    found = clues("received_1234567.jpeg", camera=None, dated=False, size=(800, 600), text=MEME)

    assert category(found) == "meme"


def test_a_camera_photo_with_big_text_is_not_a_meme() -> None:
    assert category(clues("IMG_1234.JPG", text=MEME)) is None


def test_without_vision_text_zero_shot_can_spot_a_meme() -> None:
    found = clues("image.jpg", camera=None, dated=False, text=None, labels={"meme": 0.85})

    assert category(found) == "meme"


# --- messaging forwards --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "size"),
    [("IMG-20190702-WA0003.jpg", (1600, 1200)), ("VID-20190702-WA0001.mp4", (848, 480))],
)
def test_whatsapp_forwards(name: str, size: tuple[int, int]) -> None:
    kind = "video" if name.endswith(".mp4") else "photo"
    found = clues(name, camera=None, dated=False, size=size, kind=kind)

    assert category(found) == "forward"


@pytest.mark.parametrize(
    "name",
    ["received_10157012345678901.jpeg", "FB_IMG_1562070000000.jpg", "signal-2021-02-03-040506.jpg",
     "photo_2021-02-03_04-05-06.jpg"],
)  # fmt: skip
def test_other_messaging_apps(name: str) -> None:
    found = clues(name, camera=None, dated=False, size=(1280, 960))

    assert category(found) == "forward"


def test_a_stripped_picture_at_a_messaging_size_alone_is_not_enough() -> None:
    assert category(clues("holiday.jpg", camera=None, dated=False, size=(1600, 1200))) is None


# --- Google creations ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "flags"),
    [
        ("IMG_1234-MOTION.gif", frozenset()),
        ("IMG_1234-MIX.jpg", frozenset()),
        ("IMG_1234-SMILE-edited.jpg", frozenset()),
        ("PANO_strange.jpg", frozenset({"google-creation"})),
    ],
)
def test_google_creations(name: str, flags: frozenset[str]) -> None:
    assert category(clues(name, camera=None, flags=flags)) == "google-creation"


# --- the verdict ---------------------------------------------------------------------------


def test_every_category_scores_and_the_best_wins() -> None:
    found = clues("Screenshot_20191105-123456.png", camera=None, dated=False, text=RECEIPT)

    assert set(scores(found)) == set(CATEGORIES)
    result = classify(found, min_score=0.5)
    assert result is not None
    name, score, evidence = result
    assert name == max(CATEGORIES, key=lambda c: scores(found)[c][0])
    assert 0.5 <= score <= 1
    assert evidence == scores(found)[name][1]


def test_a_higher_threshold_keeps_only_the_surest() -> None:
    weak = clues("IMG_0042.PNG", camera=None, dated=False, size=(1170, 2532))

    assert classify(weak, min_score=0.99) is None


def test_scores_stay_between_zero_and_one() -> None:
    everything = replace(
        clues("Screenshot-WA0001-MOTION.png", camera=None, dated=False, size=(1170, 2532)),
        flags=frozenset({"google-creation", "screenshot"}),
        text=TextSummary(regions=99, coverage=1.0, tallest=0.5, banded=1.0),
        labels={"document": 1.0, "meme": 1.0},
    )

    assert all(0 <= score <= 1 for score, _ in scores(everything).values())


# --- zero-shot labels ----------------------------------------------------------------------


class Prompts:
    """Text vectors along chosen axes: each label's prompts point one way."""

    def __init__(self, axes: dict[str, int]) -> None:
        self.axes = axes
        self.calls = 0

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        self.calls += 1
        vectors = []
        for text in texts:
            vector = np.zeros(4, dtype=np.float32)
            vector[next(axis for word, axis in self.axes.items() if word in text)] = 1.0
            vectors.append(vector)
        return vectors


def test_zero_shot_labels_follow_the_nearest_prompts_and_sum_to_one() -> None:
    prompts = Prompts({"receipt": 0, "document": 0, "text": 0, "letter": 0, "meme": 1,
                       "caption": 1, "funny": 1, "screen": 2, "app": 2, "photo": 3})  # fmt: skip
    zero_shot = ZeroShot(prompts)  # type: ignore[arg-type]
    receipt_like = np.array([0.9, 0.1, 0.0, 0.1], dtype=np.float32)

    labels = zero_shot.labels(receipt_like / np.linalg.norm(receipt_like))

    assert sum(labels.values()) == pytest.approx(1.0)
    assert max(labels, key=lambda k: labels[k]) == "document"
    zero_shot.labels(np.array([0, 0, 0, 1], dtype=np.float32))
    assert prompts.calls == 1, "the prompts are embedded once"


def test_one_big_text_region_is_not_a_document() -> None:
    poster = TextSummary(regions=1, coverage=0.25, tallest=0.25, banded=0.0)

    assert category(clues("IMG_4001.JPG", text=poster)) is None
    assert "text-coverage" not in scores(clues("IMG_4001.JPG", text=poster))["document"][1]


def test_a_camera_video_in_full_hd_is_not_a_screenshot() -> None:
    video = clues("IMG_0001.MOV", camera=None, dated=False, size=(1920, 1080), kind="video")

    assert category(video) is None
    assert "screen-size" not in scores(video)["screenshot"][1]


def test_a_screen_size_alone_is_not_enough() -> None:
    assert category(clues("IMG_0042.JPG", camera=None, dated=True, size=(1170, 2532))) is None
