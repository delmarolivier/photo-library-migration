"""Deterministic synthetic media: images with EXIF, animated GIFs and tiny real videos."""

import io
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone

import pytest
from PIL import ExifTags, Image, ImageChops, ImageStat

from photomigrate.testing import media
from photomigrate.testing.media import (
    Exif,
    MediaToolMissingError,
    encode_gif,
    encode_image,
    encode_video,
    reencode_video,
    synthetic_image,
    synthetic_photo,
    video_frames,
)

TAKEN = datetime(2019, 7, 1, 12, 34, 56, tzinfo=timezone(timedelta(hours=2)))


def open_image(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def mean_difference(a: Image.Image, b: Image.Image) -> float:
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB").resize(a.size))
    return sum(ImageStat.Stat(diff).mean) / 3


def test_synthetic_images_are_deterministic_per_seed() -> None:
    assert synthetic_image(7).tobytes() == synthetic_image(7).tobytes()


def test_different_seeds_give_visibly_different_images() -> None:
    assert mean_difference(synthetic_image(1), synthetic_image(2)) > 20


def test_synthetic_image_size_is_configurable() -> None:
    assert synthetic_image(1, size=(64, 48)).size == (64, 48)


@pytest.mark.parametrize(("fmt", "mime"), [("JPEG", "JPEG"), ("HEIC", "HEIF"), ("PNG", "PNG")])
def test_encode_image_formats(fmt: str, mime: str) -> None:
    data = encode_image(synthetic_image(1, size=(64, 48)), fmt)  # type: ignore[arg-type]

    assert open_image(data).format == mime


def test_jpeg_encoding_is_byte_for_byte_deterministic() -> None:
    image = synthetic_image(3)
    exif = Exif(taken=TAKEN, make="Apple", model="iPhone 8")

    assert encode_image(image, "JPEG", exif=exif) == encode_image(image, "JPEG", exif=exif)


@pytest.mark.parametrize("fmt", ["JPEG", "HEIC"])
def test_exif_capture_time_camera_and_gps_round_trip(fmt: str) -> None:
    exif = Exif(taken=TAKEN, make="Apple", model="iPhone 8", gps=(-33.8567844, 151.2152967))

    read = open_image(encode_image(synthetic_image(1), fmt, exif=exif)).getexif()  # type: ignore[arg-type]

    assert read[ExifTags.Base.Make] == "Apple"
    assert read[ExifTags.Base.Model] == "iPhone 8"
    details = read.get_ifd(ExifTags.IFD.Exif)
    assert details[ExifTags.Base.DateTimeOriginal] == "2019:07:01 12:34:56"
    assert details[ExifTags.Base.OffsetTimeOriginal] == "+02:00"
    gps = read.get_ifd(ExifTags.IFD.GPSInfo)
    assert gps[ExifTags.GPS.GPSLatitudeRef] == "S"
    assert gps[ExifTags.GPS.GPSLongitudeRef] == "E"
    degrees, minutes, seconds = (float(v) for v in gps[ExifTags.GPS.GPSLatitude])
    assert degrees + minutes / 60 + seconds / 3600 == pytest.approx(33.8567844, abs=1e-6)


def test_capture_time_without_timezone_has_no_offset_tag() -> None:
    exif = Exif(taken=datetime(2000, 1, 1, 0, 0, 0))

    details = (
        open_image(encode_image(synthetic_image(1), "JPEG", exif=exif))
        .getexif()
        .get_ifd(ExifTags.IFD.Exif)
    )

    assert details[ExifTags.Base.DateTimeOriginal] == "2000:01:01 00:00:00"
    assert ExifTags.Base.OffsetTimeOriginal not in details


def test_images_without_exif_have_no_exif() -> None:
    assert len(open_image(encode_image(synthetic_image(1), "JPEG")).getexif()) == 0


def test_lower_quality_makes_smaller_files() -> None:
    image = synthetic_image(5)

    assert len(encode_image(image, "JPEG", quality=30)) < len(encode_image(image, "JPEG"))


def test_animated_gif_keeps_every_frame() -> None:
    frames = video_frames(4, seconds=0.5, fps=10)

    gif = open_image(encode_gif(frames))

    assert gif.format == "GIF"
    assert getattr(gif, "n_frames", 1) == 5


def test_video_frames_move_over_time() -> None:
    frames = video_frames(4, seconds=1.0, fps=10, size=(64, 48))

    assert len(frames) == 10
    assert frames[0].size == (64, 48)
    assert mean_difference(frames[0], frames[-1]) > 1


def ffprobe(
    data: bytes, suffix: str, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, object]:
    path = tmp_path_factory.mktemp("probe") / f"clip{suffix}"
    path.write_bytes(data)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    result: dict[str, object] = json.loads(out)
    return result


@pytest.mark.parametrize(("container", "format_name"), [("mp4", "mp4"), ("mov", "mov")])
def test_encode_video_makes_a_real_playable_clip(
    container: str, format_name: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    created = datetime(2019, 7, 1, 10, 34, 56, tzinfo=UTC)

    data = encode_video(video_frames(1), container, created=created)  # type: ignore[arg-type]

    probe = ffprobe(data, f".{container}", tmp_path_factory)
    fmt = probe["format"]
    assert isinstance(fmt, dict)
    assert format_name in fmt["format_name"]
    assert float(fmt["duration"]) == pytest.approx(1.0, abs=0.05)
    assert fmt["tags"]["creation_time"] == "2019-07-01T10:34:56.000000Z"
    streams = probe["streams"]
    assert isinstance(streams, list)
    assert streams[0]["codec_name"] == "h264"
    assert len(data) < 100_000


def test_encode_video_is_deterministic() -> None:
    frames = video_frames(2)

    assert encode_video(frames, "mp4") == encode_video(frames, "mp4")


def test_missing_ffmpeg_is_reported_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda _name: None)

    with pytest.raises(MediaToolMissingError, match="brew install ffmpeg"):
        encode_video(video_frames(1), "mp4")


def test_ffmpeg_is_on_path_for_this_test_run() -> None:
    assert shutil.which("ffmpeg"), "install ffmpeg (brew install ffmpeg / apt install ffmpeg)"


def test_camera_exif_without_a_capture_time() -> None:
    exif = open_image(
        encode_image(synthetic_image(1), "JPEG", exif=Exif(make="Apple", model="iPhone 8"))
    ).getexif()

    assert exif[ExifTags.Base.Model] == "iPhone 8"
    assert ExifTags.Base.DateTimeOriginal not in exif.get_ifd(ExifTags.IFD.Exif)


def test_video_content_identifier_is_written_as_a_quicktime_key(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    data = encode_video(video_frames(1), "mov", content_id="3F2504E0-4F89-41D3")

    tags = ffprobe(data, ".mov", tmp_path_factory)["format"]["tags"]  # type: ignore[index]

    assert tags["com.apple.quicktime.content.identifier"] == "3F2504E0-4F89-41D3"


def test_jitter_moves_the_shapes_a_little() -> None:
    still = synthetic_image(5)

    assert synthetic_image(5, jitter=0).tobytes() == still.tobytes()
    moved = synthetic_image(5, jitter=3)
    assert moved.tobytes() != still.tobytes()
    assert mean_difference(still, moved) < mean_difference(still, synthetic_image(6))


def test_reencode_video_can_downscale_and_lower_quality(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    original = encode_video(video_frames(3, seconds=2.0, size=(128, 96)), "mp4")

    smaller = reencode_video(original, "mp4", scale=(64, 48), crf=40)

    probe = ffprobe(smaller, ".mp4", tmp_path_factory)
    stream = probe["streams"][0]  # type: ignore[index]
    assert (stream["width"], stream["height"]) == (64, 48)
    assert float(probe["format"]["duration"]) == pytest.approx(2.0, abs=0.1)  # type: ignore[index]
    assert len(smaller) < len(original)
    assert reencode_video(original, "mov") != original


def test_reencoding_without_ffmpeg_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda _name: None)

    with pytest.raises(MediaToolMissingError, match="re-encode"):
        reencode_video(b"", "mp4")


def test_synthetic_photos_have_fine_texture_like_a_real_picture() -> None:
    photo = synthetic_photo(3, size=(200, 100))

    assert photo.size == (200, 100)
    assert photo.tobytes() == synthetic_photo(3, size=(200, 100)).tobytes()
    flat = synthetic_image(3, size=(200, 100))
    assert 2 < mean_difference(photo, flat) < 30, "texture on top of the same scene"
