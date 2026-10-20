"""exiftool layer: read embedded metadata, and write planned metadata into a *new* file."""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from photomigrate.ingest.exif import (
    Embedded,
    ExifTool,
    MetadataError,
    MetadataWrite,
    can_write,
    embedded_from_tags,
    parse_exif_time,
)
from photomigrate.safety import ForbiddenPathError, GuardRules, PathGuard
from photomigrate.testing.media import (
    encode_gif,
    encode_image,
    encode_video,
    synthetic_image,
    video_frames,
)

PARIS = timezone(timedelta(hours=2))
TAKEN = datetime(2019, 7, 1, 12, 34, 56, tzinfo=PARIS)
PLAN = MetadataWrite(
    taken=TAKEN,
    latitude=-33.8567844,
    longitude=151.2152967,
    altitude=12.5,
    description="Plage à Nice 🏖️",
)


def make(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    image = synthetic_image(1, (64, 48))
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".heic", ".png"):
        path.write_bytes(
            encode_image(image, {".jpg": "JPEG", ".heic": "HEIC", ".png": "PNG"}[suffix])
        )  # type: ignore[arg-type]
    elif suffix == ".gif":
        path.write_bytes(encode_gif(video_frames(1, 0.3)))
    else:
        path.write_bytes(encode_video(video_frames(1), suffix[1:]))  # type: ignore[arg-type]
    return path


@pytest.fixture(scope="module")
def exiftool() -> ExifTool:
    with ExifTool() as tool:
        yield tool  # type: ignore[misc]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", ["a.jpg", "a.heic", "a.png", "a.gif", "a.mov", "a.mp4"])
def test_written_metadata_reads_back_identical(
    exiftool: ExifTool, tmp_path: Path, name: str
) -> None:
    source = make(tmp_path, name)
    before = sha(source)
    dest = tmp_path / "out" / name
    dest.parent.mkdir()

    exiftool.write(source, dest, PLAN)
    read = exiftool.read(dest)

    assert read.taken == TAKEN
    assert read.taken is not None
    assert read.taken.utcoffset() == timedelta(hours=2)
    assert read.latitude == pytest.approx(PLAN.latitude, abs=1e-6)
    assert read.longitude == pytest.approx(PLAN.longitude, abs=1e-6)
    assert read.description == "Plage à Nice 🏖️"
    assert sha(source) == before, "the source must never be modified"


@pytest.mark.parametrize("name", ["a.jpg", "a.heic", "a.mov"])
def test_altitude_round_trips(exiftool: ExifTool, tmp_path: Path, name: str) -> None:
    dest = tmp_path / f"out-{name}"

    exiftool.write(make(tmp_path, name), dest, PLAN)

    assert exiftool.read(dest).altitude == pytest.approx(12.5, abs=0.01)


def test_only_the_planned_fields_are_written(exiftool: ExifTool, tmp_path: Path) -> None:
    dest = tmp_path / "out.jpg"

    exiftool.write(make(tmp_path, "a.jpg"), dest, MetadataWrite(description="Only a caption"))
    read = exiftool.read(dest)

    assert read.description == "Only a caption"
    assert read.taken is None
    assert read.latitude is None


def test_a_bare_image_has_no_metadata_but_has_a_size(exiftool: ExifTool, tmp_path: Path) -> None:
    read = exiftool.read(make(tmp_path, "a.jpg"))

    assert read == Embedded(width=64, height=48)


def test_video_duration_and_capture_time(exiftool: ExifTool, tmp_path: Path) -> None:
    clip = tmp_path / "clip.mov"
    clip.write_bytes(encode_video(video_frames(1), "mov", created=TAKEN))

    read = exiftool.read(clip)

    assert read.taken == TAKEN
    assert read.duration == pytest.approx(1.0, abs=0.05)
    assert (read.width, read.height) == (64, 48)


def test_a_zero_quicktime_date_counts_as_missing(exiftool: ExifTool, tmp_path: Path) -> None:
    assert exiftool.read(make(tmp_path, "a.mp4")).taken is None


def test_exif_date_without_offset_reads_back_naive(exiftool: ExifTool, tmp_path: Path) -> None:
    path = tmp_path / "a.jpg"
    from photomigrate.testing.media import Exif

    path.write_bytes(
        encode_image(synthetic_image(1), "JPEG", exif=Exif(taken=datetime(2000, 1, 1, 0, 3)))
    )

    assert exiftool.read(path).taken == datetime(2000, 1, 1, 0, 3)


def test_write_refuses_an_existing_destination(exiftool: ExifTool, tmp_path: Path) -> None:
    source = make(tmp_path, "a.jpg")
    dest = tmp_path / "taken.jpg"
    dest.write_bytes(b"already here")

    with pytest.raises(MetadataError, match="exists"):
        exiftool.write(source, dest, PLAN)

    assert dest.read_bytes() == b"already here"


def test_exiftool_failures_are_reported(exiftool: ExifTool, tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.jpg"
    bogus.write_bytes(b"not an image")

    with pytest.raises(MetadataError):
        exiftool.write(bogus, tmp_path / "out.jpg", PLAN)


def test_an_empty_plan_or_a_naive_time_is_rejected(exiftool: ExifTool, tmp_path: Path) -> None:
    source = make(tmp_path, "a.jpg")

    with pytest.raises(ValueError, match="nothing"):
        exiftool.write(source, tmp_path / "x.jpg", MetadataWrite())
    with pytest.raises(ValueError, match="timezone"):
        MetadataWrite(taken=datetime(2019, 7, 1, 12, 0))
    with pytest.raises(ValueError, match="together"):
        MetadataWrite(latitude=1.0)
    with pytest.raises(ValueError, match="altitude"):
        MetadataWrite(altitude=3.0)


def test_unwritable_formats_are_refused(exiftool: ExifTool, tmp_path: Path) -> None:
    clip = tmp_path / "a.avi"
    clip.write_bytes(b"RIFF")

    with pytest.raises(MetadataError, match="cannot write"):
        exiftool.write(clip, tmp_path / "out.avi", PLAN)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.JPG", True), ("a.heic", True), ("a.gif", True), ("a.MOV", True), ("a.mp4", True),
     ("a.avi", False), ("a.mkv", False), ("a.cr2", False), ("a.txt", False)],
)  # fmt: skip
def test_can_write(name: str, expected: bool) -> None:
    assert can_write(name) is expected


def test_plans_round_trip_through_plain_dicts() -> None:
    assert MetadataWrite.from_dict(PLAN.to_dict()) == PLAN
    assert MetadataWrite.from_dict({}) == MetadataWrite()
    assert not MetadataWrite()
    assert PLAN


def test_paths_sent_to_exiftool_over_stdin_are_still_guarded(
    exiftool: ExifTool, tmp_path: Path
) -> None:
    protected = tmp_path / "Pictures"
    protected.mkdir()
    photo = make(protected, "a.jpg")
    guard = PathGuard(GuardRules(forbidden_roots=(protected,), allowed_roots=()))

    guard.activate()
    try:
        with pytest.raises(ForbiddenPathError):
            exiftool.read(photo)
    finally:
        guard.deactivate()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2019:07:01 12:34:56", datetime(2019, 7, 1, 12, 34, 56)),
        ("2019:07:01 12:34:56+02:00", TAKEN),
        ("2019:07:01 10:34:56Z", TAKEN),
        ("2019:07:01 12:34:56.250+02:00", TAKEN.replace(microsecond=250000)),
        ("0000:00:00 00:00:00", None),
        ("2019:13:45 25:00:00", None),
        ("yesterday", None),
        (None, None),
    ],
)
def test_parse_exif_time(text: object, expected: datetime | None) -> None:
    assert parse_exif_time(text) == expected


def test_tags_that_are_not_numbers_are_ignored() -> None:
    read = embedded_from_tags({"Composite:GPSLatitude": "n/a", "QuickTime:Duration": "1.5 s"})

    assert read.latitude is None
    assert read.duration is None


@pytest.mark.parametrize("name", ["a.jpg", "a.gif", "a.mov"])
@pytest.mark.parametrize(
    "plan",
    [
        MetadataWrite(taken=TAKEN),
        MetadataWrite(latitude=48.8584, longitude=2.2945),
        MetadataWrite(latitude=-0.5, longitude=-60.25, altitude=-3.0),
        MetadataWrite(description="Tour Eiffel"),
    ],
    ids=["date", "gps", "altitude", "caption"],
)
def test_each_field_can_be_written_on_its_own(
    exiftool: ExifTool, tmp_path: Path, name: str, plan: MetadataWrite
) -> None:
    dest = tmp_path / f"out-{name}"

    exiftool.write(make(tmp_path, name), dest, plan)
    read = exiftool.read(dest)

    assert read.taken == plan.taken
    if plan.latitude is not None:
        assert read.latitude == pytest.approx(plan.latitude, abs=1e-6)
    if plan.description is not None:
        assert read.description == plan.description


def test_reading_a_missing_file_is_reported(exiftool: ExifTool, tmp_path: Path) -> None:
    with pytest.raises(MetadataError, match="could not read"):
        exiftool.read(tmp_path / "missing.jpg")


def test_closing_twice_is_harmless() -> None:
    tool = ExifTool()
    tool.close()
    with tool:
        pass
    tool.close()


@pytest.mark.parametrize(
    "tags",
    [
        {"QuickTime:CreateDate": "1904:01:01 00:00:00Z"},
        {"QuickTime:CreateDate": "1904:01:01 00:00:00"},
        {"Keys:CreationDate": "1904:01:01 01:00:00+01:00"},
    ],
)
def test_the_quicktime_epoch_means_no_date(tags: dict[str, str]) -> None:
    # Older exiftool versions (Ubuntu's) report an unset QuickTime date as its 1904 epoch.
    assert embedded_from_tags(tags).taken is None
