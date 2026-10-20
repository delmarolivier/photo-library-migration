"""Quality measurements of photos and videos (raw numbers; thresholds are applied later)."""

from pathlib import Path

import pytest
from PIL import Image, ImageEnhance, ImageFilter

from photomigrate.hashing import sample_video, signature_of
from photomigrate.quality import measure_photo, measure_video, photo_metrics, video_metrics
from photomigrate.testing.media import encode_image, encode_video, synthetic_image, video_frames


def jpeg(tmp_path: Path, name: str, image: Image.Image) -> Path:
    path = tmp_path / name
    path.write_bytes(encode_image(image, "JPEG", quality=92))
    return path


def test_blur_lowers_sharpness(tmp_path: Path) -> None:
    image = synthetic_image(1, size=(800, 600))
    sharp = photo_metrics(jpeg(tmp_path, "sharp.jpg", image))
    soft = photo_metrics(jpeg(tmp_path, "soft.jpg", image.filter(ImageFilter.GaussianBlur(6))))

    assert sharp is not None
    assert soft is not None
    assert sharp.sharpness > 10 * soft.sharpness


def test_dark_and_bright_pictures(tmp_path: Path) -> None:
    image = synthetic_image(2, size=(800, 600))
    normal = photo_metrics(jpeg(tmp_path, "n.jpg", image))
    dark = photo_metrics(jpeg(tmp_path, "d.jpg", ImageEnhance.Brightness(image).enhance(0.12)))
    washed_out = Image.blend(image, Image.new("RGB", image.size, "white"), 0.95)
    bright = photo_metrics(jpeg(tmp_path, "b.jpg", washed_out))

    assert normal is not None
    assert dark is not None
    assert bright is not None
    assert dark.brightness < 40 < normal.brightness < 215 < bright.brightness
    assert dark.dark_fraction > 0.5
    assert bright.bright_fraction > 0.5


def test_a_pocket_shot_is_dark_and_flat(tmp_path: Path) -> None:
    metrics = photo_metrics(jpeg(tmp_path, "pocket.jpg", Image.new("RGB", (800, 600), (6, 5, 7))))

    assert metrics is not None
    assert metrics.brightness < 10
    assert metrics.contrast < 2


def test_size_is_reported_in_megapixels(tmp_path: Path) -> None:
    metrics = photo_metrics(jpeg(tmp_path, "small.jpg", synthetic_image(3, size=(320, 240))))

    assert metrics is not None
    assert metrics.megapixels == pytest.approx(0.0768)
    assert metrics.short_side == 240


def test_metrics_are_measured_on_the_upright_image(tmp_path: Path) -> None:
    metrics = photo_metrics(jpeg(tmp_path, "tall.jpg", synthetic_image(4, size=(300, 900))))

    assert metrics is not None
    assert metrics.short_side == 300


def test_metrics_round_trip_through_plain_dicts(tmp_path: Path) -> None:
    from photomigrate.quality import PhotoMetrics

    metrics = photo_metrics(jpeg(tmp_path, "a.jpg", synthetic_image(5)))

    assert metrics is not None
    assert PhotoMetrics.from_dict(metrics.to_dict()) == metrics


def test_undecodable_files_have_no_metrics(tmp_path: Path) -> None:
    (tmp_path / "x.jpg").write_bytes(b"nope")

    assert photo_metrics(tmp_path / "x.jpg") is None
    assert measure_photo(tmp_path / "x.jpg") == (None, None)


def test_hashes_and_metrics_come_from_one_decode(tmp_path: Path) -> None:
    from photomigrate.hashing import image_hashes

    path = jpeg(tmp_path, "a.jpg", synthetic_image(8, size=(1600, 1200)))

    assert measure_photo(path) == (image_hashes(path), photo_metrics(path))


def clip(tmp_path: Path, frames: list[Image.Image], name: str = "c.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(encode_video(frames, "mp4"))
    return path


def test_video_brightness_and_motion(tmp_path: Path) -> None:
    moving = sample_video(clip(tmp_path, video_frames(1, seconds=2.0)), 2.0)
    black = sample_video(clip(tmp_path, [Image.new("RGB", (64, 48), (2, 2, 2))] * 20, "b.mp4"), 2.0)
    still = sample_video(clip(tmp_path, [synthetic_image(9, (64, 48))] * 20, "s.mp4"), 2.0)

    assert moving is not None
    assert black is not None
    assert still is not None
    m, b, s = video_metrics(moving), video_metrics(black), video_metrics(still)
    assert b.brightness < 10 < m.brightness
    assert s.motion < 0.5 < m.motion
    assert type(m).from_dict(m.to_dict()) == m


def test_the_signature_comes_from_the_same_sampled_frames(tmp_path: Path) -> None:
    from photomigrate.hashing import video_signature

    path = clip(tmp_path, video_frames(2, seconds=2.0))
    frames = sample_video(path, 2.0)

    assert frames is not None
    assert signature_of(frames) == video_signature(path, 2.0)
    assert sample_video(path, None) is None
    assert measure_video(path, None) == (None, None)


def test_sideways_photos_report_their_upright_size(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # stored landscape, shown portrait
    path = tmp_path / "turned.jpg"
    synthetic_image(6, size=(900, 300)).save(path, "JPEG", exif=exif.tobytes())

    metrics = photo_metrics(path)

    assert metrics is not None
    assert metrics.short_side == 300
    assert metrics.megapixels == pytest.approx(0.27)
