"""Video dedupe: re-encoded and downscaled copies match, different clips do not (CLA-81)."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from photomigrate.analyse.dedupe_video import (
    VideoDedupeConfig,
    VideoRecord,
    find_video_duplicates,
    video_keeper_order,
)
from photomigrate.hashing import bitrate, video_signature
from photomigrate.testing.media import encode_video, reencode_video, video_frames

T = datetime(2019, 7, 4, 10, 10, 10, tzinfo=UTC)
_ids = iter(range(1, 10_000))


def record(
    tmp_path: Path,
    data: bytes,
    *,
    source: str = "google",
    duration: float = 2.0,
    size: tuple[int, int] = (128, 96),
    taken: datetime | None = T,
    key: str | None = None,
) -> VideoRecord:
    item_id = next(_ids)
    path = tmp_path / f"{item_id}.mp4"
    path.write_bytes(data)
    signature = video_signature(path, duration)
    assert signature is not None
    return VideoRecord(
        id=item_id,
        source=source,  # type: ignore[arg-type]
        key=key or f"{source}-{item_id}",
        sha256=hashlib.sha256(data).hexdigest(),
        signature=tuple(signature),
        duration=duration,
        width=size[0],
        height=size[1],
        bitrate=bitrate(len(data), duration),
        taken=taken,
        has_date=taken is not None,
        has_gps=False,
        has_caption=False,
    )


def clip(seed: int, seconds: float = 2.0, size: tuple[int, int] = (128, 96)) -> bytes:
    return encode_video(video_frames(seed, seconds=seconds, size=size), "mp4", created=T)


def groups(records: list[VideoRecord], config: VideoDedupeConfig | None = None) -> list[set[int]]:
    return [set(g.members) for g in find_video_duplicates(records, config or VideoDedupeConfig())]


@pytest.mark.parametrize(
    "variant", ["identical-bytes", "reencoded", "downscaled", "lower-quality-mov"]
)
def test_copies_of_one_clip_are_grouped(tmp_path: Path, variant: str) -> None:
    data = clip(1)
    original = record(tmp_path, data, source="icloud")
    if variant == "identical-bytes":
        copy = replace(original, id=next(_ids), source="google", key="g")
    elif variant == "reencoded":
        copy = record(tmp_path, reencode_video(data, "mp4", crf=30))
    elif variant == "downscaled":
        copy = record(tmp_path, reencode_video(data, "mp4", scale=(64, 48)), size=(64, 48))
    else:
        copy = record(tmp_path, reencode_video(data, "mov", scale=(96, 72), crf=40), size=(96, 72))

    (group,) = find_video_duplicates([original, copy], VideoDedupeConfig())

    assert set(group.members) == {original.id, copy.id}
    assert group.method == ("exact" if variant == "identical-bytes" else "near")


def test_different_clips_of_the_same_length_are_not_matched(tmp_path: Path) -> None:
    records = [record(tmp_path, clip(seed)) for seed in range(10, 18)]

    assert groups(records) == []


def test_durations_more_than_half_a_second_apart_are_not_matched(tmp_path: Path) -> None:
    long = record(tmp_path, clip(2, seconds=3.0), duration=3.0)
    short = record(tmp_path, clip(2, seconds=2.0), duration=2.0)

    assert groups([long, short]) == []


def test_different_aspect_ratios_are_not_matched(tmp_path: Path) -> None:
    data = clip(3)
    a = record(tmp_path, data)
    b = record(tmp_path, reencode_video(data, "mp4", scale=(128, 72)), size=(128, 72))

    assert groups([a, b]) == []


def test_different_capture_times_are_not_matched(tmp_path: Path) -> None:
    data = clip(4)
    a = record(tmp_path, data)
    b = replace(
        record(tmp_path, reencode_video(data, "mp4", crf=30)), taken=T + timedelta(minutes=5)
    )

    assert groups([a, b]) == []


def test_videos_without_signatures_only_match_exactly(tmp_path: Path) -> None:
    a = record(tmp_path, clip(5))
    blind = replace(a, id=next(_ids), key="blind", signature=())

    assert groups([a, blind]) == [{a.id, blind.id}]
    assert groups([a, replace(blind, sha256=None)]) == []


def test_the_signature_threshold_is_configurable(tmp_path: Path) -> None:
    data = clip(6)
    a = record(tmp_path, data)
    b = record(tmp_path, reencode_video(data, "mp4", crf=35))

    assert groups([a, b]) == [{a.id, b.id}]
    assert groups([a, b], VideoDedupeConfig(signature_max=-1)) == []


# --- keeper -------------------------------------------------------------------------------


def base(tmp_path: Path) -> VideoRecord:
    return record(tmp_path, clip(7))


def test_the_keeper_is_the_highest_resolution(tmp_path: Path) -> None:
    big = replace(base(tmp_path), width=1920, height=1080, bitrate=1, key="big")
    small = replace(big, id=next(_ids), width=1280, height=720, bitrate=10**9, key="small")

    assert video_keeper_order([small, big])[0] is big


def test_then_the_highest_bitrate(tmp_path: Path) -> None:
    low = replace(base(tmp_path), bitrate=2_000_000, key="low", has_gps=True, has_caption=True)
    high = replace(
        low, id=next(_ids), bitrate=8_000_000, key="high", has_gps=False, has_caption=False
    )

    assert video_keeper_order([low, high])[0] is high


def test_then_the_richest_metadata_then_icloud_then_the_key(tmp_path: Path) -> None:
    plain = replace(base(tmp_path), source="icloud", key="plain")
    rich = replace(plain, id=next(_ids), source="google", key="rich", has_gps=True)
    google = replace(plain, id=next(_ids), source="google", key="a-google")

    assert [r.key for r in video_keeper_order([plain, google, rich])] == [
        "rich",
        "plain",
        "a-google",
    ]
    twin = replace(plain, id=next(_ids), key="a-plain")
    assert video_keeper_order([plain, twin])[0] is twin


def test_unknown_sizes_and_bitrates_rank_last(tmp_path: Path) -> None:
    known = replace(base(tmp_path), key="known")
    unknown = replace(known, id=next(_ids), width=None, height=None, bitrate=None, key="unknown")

    assert video_keeper_order([unknown, known])[0] is known


def test_unknown_sizes_skip_the_aspect_check(tmp_path: Path) -> None:
    data = clip(8)
    a = replace(record(tmp_path, data), width=None, height=None)
    b = record(tmp_path, reencode_video(data, "mp4", crf=30))

    assert groups([a, b]) == [{a.id, b.id}]
