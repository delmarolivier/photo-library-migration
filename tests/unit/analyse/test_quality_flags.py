"""Quality flags: measurements compared with the configured thresholds, with severity scores."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from photomigrate.analyse.quality import photo_flags, video_flags
from photomigrate.config import PhotoThresholds, VideoThresholds
from PIL import Image, ImageEnhance, ImageFilter

from photomigrate.hashing import sample_video
from photomigrate.quality import PhotoMetrics, VideoMetrics, photo_metrics, video_metrics
from photomigrate.testing.media import encode_image, encode_video, synthetic_photo, video_frames

PHOTO = PhotoThresholds()
VIDEO = VideoThresholds()


def measure(tmp_path: Path, image: Image.Image) -> PhotoMetrics:
    path = tmp_path / f"{id(image)}.jpg"
    path.write_bytes(encode_image(image, "JPEG", quality=92))
    metrics = photo_metrics(path)
    assert metrics is not None
    return metrics


@pytest.fixture
def sharp() -> Image.Image:
    return synthetic_photo(11, size=(800, 600))


def test_a_good_photo_has_no_flags(tmp_path: Path, sharp: Image.Image) -> None:
    assert photo_flags(measure(tmp_path, sharp), PHOTO) == {}


@pytest.mark.parametrize(
    ("change", "flag"),
    [
        (lambda im: im.filter(ImageFilter.GaussianBlur(4)), "blurry"),
        (lambda im: ImageEnhance.Brightness(im).enhance(0.25), "under-exposed"),
        (lambda im: Image.blend(im, Image.new("RGB", im.size, "white"), 0.8), "over-exposed"),
        (lambda im: im.resize((320, 240)), "low-resolution"),
        (lambda im: Image.new("RGB", im.size, (6, 5, 7)), "accidental"),
    ],
)
def test_each_problem_lands_past_its_threshold(
    tmp_path: Path, sharp: Image.Image, change: Callable[[Image.Image], Image.Image], flag: str
) -> None:
    flags = photo_flags(measure(tmp_path, change(sharp)), PHOTO)

    assert flag in flags
    assert 0 < flags[flag] <= 1


def test_only_the_problem_at_hand_is_flagged(tmp_path: Path, sharp: Image.Image) -> None:
    assert set(
        photo_flags(measure(tmp_path, sharp.filter(ImageFilter.GaussianBlur(4))), PHOTO)
    ) == {"blurry"}
    assert set(photo_flags(measure(tmp_path, sharp.resize((320, 240))), PHOTO)) == {
        "low-resolution"
    }


def test_worse_means_a_higher_score(tmp_path: Path, sharp: Image.Image) -> None:
    soft = photo_flags(measure(tmp_path, sharp.filter(ImageFilter.GaussianBlur(3))), PHOTO)
    softer = photo_flags(measure(tmp_path, sharp.filter(ImageFilter.GaussianBlur(8))), PHOTO)

    assert softer["blurry"] > soft["blurry"]


def test_thresholds_decide_which_side_a_photo_is_on(tmp_path: Path, sharp: Image.Image) -> None:
    metrics = measure(tmp_path, sharp)
    strict = replace(PHOTO, blurry_below_sharpness=metrics.sharpness + 1)

    assert set(photo_flags(metrics, strict)) == {"blurry"}


def test_a_histogram_mostly_crushed_or_blown_is_badly_exposed() -> None:
    base = PhotoMetrics(
        sharpness=500, brightness=120, dark_fraction=0, bright_fraction=0,
        contrast=60, megapixels=12, short_side=3000,
    )  # fmt: skip

    assert photo_flags(base, PHOTO) == {}
    assert set(photo_flags(replace(base, dark_fraction=0.6), PHOTO)) == {"under-exposed"}
    assert set(photo_flags(replace(base, bright_fraction=0.7), PHOTO)) == {"over-exposed"}


def test_unmeasured_photos_have_no_flags() -> None:
    assert photo_flags(None, PHOTO) == {}


def clip_metrics(tmp_path: Path, frames: list[Image.Image], seconds: float) -> VideoMetrics:
    path = tmp_path / f"{id(frames)}.mp4"
    path.write_bytes(encode_video(frames, "mp4"))
    sampled = sample_video(path, seconds)
    assert sampled is not None
    return video_metrics(sampled)


def test_a_good_video_has_no_flags(tmp_path: Path) -> None:
    metrics = clip_metrics(tmp_path, video_frames(1, seconds=2.0, size=(320, 240)), 2.0)

    assert video_flags(metrics, duration=2.0, short_side=240, motion_half=False, t=VIDEO) == {}


def test_black_static_tiny_and_short_videos(tmp_path: Path) -> None:
    black = clip_metrics(tmp_path, [Image.new("RGB", (64, 48), (2, 2, 2))] * 20, 2.0)
    still = clip_metrics(tmp_path, [synthetic_photo(9, (320, 240))] * 20, 2.0)
    moving = clip_metrics(tmp_path, video_frames(1, seconds=2.0, size=(320, 240)), 2.0)

    assert {"black-video", "static-video"} <= set(
        video_flags(black, duration=2.0, short_side=240, motion_half=False, t=VIDEO)
    )
    assert set(video_flags(still, duration=2.0, short_side=240, motion_half=False, t=VIDEO)) == {
        "static-video"
    }
    assert set(video_flags(moving, duration=2.0, short_side=48, motion_half=False, t=VIDEO)) == {
        "tiny-video"
    }
    assert set(video_flags(moving, duration=0.8, short_side=240, motion_half=False, t=VIDEO)) == {
        "short-clip"
    }


def test_live_photo_motion_is_not_a_short_clip() -> None:
    fine = VideoMetrics(brightness=100, motion=20)

    assert video_flags(fine, duration=1.2, short_side=1080, motion_half=True, t=VIDEO) == {}


def test_unmeasured_videos_are_still_checked_for_length_and_size() -> None:
    flags = video_flags(None, duration=0.5, short_side=120, motion_half=False, t=VIDEO)

    assert set(flags) == {"short-clip", "tiny-video"}
    assert video_flags(None, duration=None, short_side=None, motion_half=False, t=VIDEO) == {}


def test_video_thresholds_come_from_the_config() -> None:
    fine = VideoMetrics(brightness=100, motion=20)
    picky = replace(VIDEO, short_below_seconds=10.0)

    assert set(video_flags(fine, duration=5.0, short_side=1080, motion_half=False, t=picky)) == {
        "short-clip"
    }
