"""Photo dedupe: exact and perceptual duplicates, with crops and other shots kept apart."""

import io
import random
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from photomigrate.analyse.dedupe_photo import (
    DedupeConfig,
    PhotoRecord,
    find_duplicates,
    keeper_order,
    near_pairs,
)
from photomigrate.hashing import image_hashes
from photomigrate.testing.media import Exif, encode_image, synthetic_image

T = datetime(2019, 7, 1, 12, 34, 56)


def scene(seed: int, jitter: int = 0) -> Image.Image:
    return synthetic_image(seed, jitter=jitter)


_ids = iter(range(1, 10_000))


def record(
    tmp_path: Path,
    image: Image.Image,
    *,
    source: str = "google",
    fmt: str = "JPEG",
    quality: int = 85,
    taken: datetime | None = T,
    gps: bool = False,
    caption: bool = False,
    key: str | None = None,
) -> PhotoRecord:
    item_id = next(_ids)
    data = encode_image(image, fmt, quality=quality, exif=Exif(taken=taken) if taken else None)  # type: ignore[arg-type]
    path = tmp_path / f"{item_id}.{fmt.lower()}"
    path.write_bytes(data)
    hashes = image_hashes(path)
    assert hashes is not None
    import hashlib

    return PhotoRecord(
        id=item_id,
        source=source,  # type: ignore[arg-type]
        key=key or f"{source}-{item_id}",
        sha256=hashlib.sha256(data).hexdigest(),
        phash=hashes.phash,
        dhash=hashes.dhash,
        fine=hashes.fine,
        width=image.width,
        height=image.height,
        taken=taken,
        content_id=None,
        has_date=taken is not None,
        has_gps=gps,
        has_caption=caption,
    )


def groups_of(records: list[PhotoRecord], config: DedupeConfig | None = None) -> list[set[int]]:
    return [set(g.members) for g in find_duplicates(records, config or DedupeConfig())]


# --- recall: copies are caught ------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    ["identical-bytes", "resized", "recompressed", "heic-vs-jpeg", "stripped-exif"],
)
def test_copies_of_one_photo_are_grouped(tmp_path: Path, variant: str) -> None:
    image = scene(1)
    original = record(tmp_path, image, source="icloud")
    if variant == "identical-bytes":
        copy = replace(original, id=next(_ids), source="google", key="g")
    elif variant == "resized":
        copy = record(tmp_path, image.resize((160, 120)))
    elif variant == "recompressed":
        copy = record(tmp_path, image, quality=25)
    elif variant == "heic-vs-jpeg":
        copy = record(tmp_path, image, fmt="HEIC")
    else:
        copy = record(tmp_path, image.resize((200, 150)), taken=None)

    (group,) = find_duplicates([original, copy], DedupeConfig())

    assert set(group.members) == {original.id, copy.id}
    assert group.method == ("exact" if variant == "identical-bytes" else "near")


def test_duplicates_within_one_source_are_grouped(tmp_path: Path) -> None:
    image = scene(2)
    a = record(tmp_path, image, source="icloud")
    b = record(tmp_path, image, source="icloud", quality=40)

    assert groups_of([a, b]) == [{a.id, b.id}]


def test_a_shared_live_photo_identifier_is_a_duplicate(tmp_path: Path) -> None:
    a = replace(record(tmp_path, scene(3), source="icloud"), content_id="CI")
    b = replace(record(tmp_path, scene(4)), content_id="CI")

    (group,) = find_duplicates([a, b], DedupeConfig())

    assert group.method == "content-id"


def test_groups_merge_transitively(tmp_path: Path) -> None:
    image = scene(5)
    a = record(tmp_path, image, source="icloud")
    b = record(tmp_path, image, quality=30)
    c = replace(b, id=next(_ids), key="exact-copy-of-b")

    (group,) = find_duplicates([a, b, c], DedupeConfig())

    assert set(group.members) == {a.id, b.id, c.id}
    assert group.method == "mixed"


# --- precision: crops and other shots stay apart --------------------------------------------


@pytest.mark.parametrize("box", [(8, 6, 312, 234), (16, 12, 304, 228), (32, 24, 288, 216)])
def test_crops_are_not_duplicates(tmp_path: Path, box: tuple[int, int, int, int]) -> None:
    image = scene(6)
    original = record(tmp_path, image)
    crop = record(tmp_path, image.crop(box))

    assert groups_of([original, crop]) == []


@pytest.mark.parametrize("seed", range(10, 20))
def test_a_moment_later_is_another_shot(tmp_path: Path, seed: int) -> None:
    first = record(tmp_path, scene(seed), taken=T)
    second = record(
        tmp_path, scene(seed, jitter=3), taken=T + timedelta(seconds=1, milliseconds=600)
    )

    assert groups_of([first, second]) == []


def test_same_scene_with_movement_and_no_dates_is_another_shot(tmp_path: Path) -> None:
    first = record(tmp_path, scene(30), taken=None)
    second = record(tmp_path, scene(30, jitter=15), taken=None)

    assert groups_of([first, second]) == []


def test_different_aspect_ratios_are_not_duplicates(tmp_path: Path) -> None:
    image = scene(7)
    a = record(tmp_path, image)
    b = record(tmp_path, image.resize((320, 180)))

    assert groups_of([a, b]) == []


def test_rotation_does_not_change_the_aspect_check(tmp_path: Path) -> None:
    image = scene(8)
    a = record(tmp_path, image)
    b = replace(record(tmp_path, image, quality=50), width=240, height=320)  # stored sideways

    assert groups_of([a, b]) == [{a.id, b.id}]


def test_distinct_photos_are_left_alone(tmp_path: Path) -> None:
    records = [record(tmp_path, scene(100 + n)) for n in range(12)]

    assert groups_of(records) == []


def test_thresholds_are_configurable(tmp_path: Path) -> None:
    image = scene(9)
    a = record(tmp_path, image)
    b = record(tmp_path, image, quality=20)

    assert groups_of([a, b]) == [{a.id, b.id}]
    assert groups_of([a, b], DedupeConfig(fine_max=-1)) == []


def test_records_without_hashes_only_match_exactly(tmp_path: Path) -> None:
    a = record(tmp_path, scene(11))
    blind = replace(a, id=next(_ids), key="blind", phash=None, dhash=None, fine=None)

    assert groups_of([a, blind]) == [{a.id, blind.id}]
    assert groups_of([replace(blind, sha256=None), a]) == []


# --- keeper -------------------------------------------------------------------------------


def base(tmp_path: Path) -> PhotoRecord:
    return record(tmp_path, scene(40))


def test_the_keeper_is_the_highest_resolution(tmp_path: Path) -> None:
    big = replace(base(tmp_path), width=4032, height=3024, key="big")
    small = replace(
        big, id=next(_ids), width=1600, height=1200, key="small", has_gps=True, has_caption=True
    )

    assert keeper_order([small, big])[0] is big


def test_then_the_richest_metadata(tmp_path: Path) -> None:
    plain = replace(base(tmp_path), key="plain", has_date=True)
    rich = replace(plain, id=next(_ids), key="rich", has_gps=True, has_caption=True)
    dated = replace(plain, id=next(_ids), key="dated", has_gps=True)

    assert [r.key for r in keeper_order([plain, dated, rich])] == ["rich", "dated", "plain"]


def test_then_the_copy_already_in_icloud(tmp_path: Path) -> None:
    google = replace(base(tmp_path), source="google", key="a-google")
    icloud = replace(google, id=next(_ids), source="icloud", key="z-icloud")

    assert keeper_order([google, icloud])[0] is icloud


def test_finally_the_key_breaks_ties(tmp_path: Path) -> None:
    a = replace(base(tmp_path), key="a")
    b = replace(a, id=next(_ids), key="b")

    assert keeper_order([b, a])[0] is a


def test_unknown_sizes_rank_last(tmp_path: Path) -> None:
    known = replace(base(tmp_path), key="known")
    unknown = replace(known, id=next(_ids), width=None, height=None, key="unknown", has_gps=True)

    assert keeper_order([unknown, known])[0] is known


def test_groups_list_the_keeper_first(tmp_path: Path) -> None:
    image = scene(41)
    small = record(tmp_path, image.resize((160, 120)), source="google")
    big = record(tmp_path, image, source="icloud")

    (group,) = find_duplicates([small, big], DedupeConfig())

    assert group.keeper == big.id
    assert group.members == (big.id, small.id)


# --- candidate search -----------------------------------------------------------------------


@given(st.lists(st.integers(0, 2**64 - 1), max_size=40), st.integers(0, 12), st.integers(1, 7))
def test_near_pairs_matches_brute_force(hashes: list[int], radius: int, block: int) -> None:
    expected = {
        (i, j, (hashes[i] ^ hashes[j]).bit_count())
        for i in range(len(hashes))
        for j in range(i + 1, len(hashes))
        if (hashes[i] ^ hashes[j]).bit_count() <= radius
    }

    assert set(near_pairs(hashes, radius, block=block)) == expected


def test_near_pairs_finds_close_hashes_among_many() -> None:
    rng = random.Random(1)
    hashes = [rng.getrandbits(64) for _ in range(3000)]
    hashes.append(hashes[123] ^ 0b101)

    assert (123, 3000, 2) in near_pairs(hashes, 3)


def test_png_and_other_formats_work_too(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    scene(50).save(buffer, "PNG")
    (tmp_path / "x.png").write_bytes(buffer.getvalue())

    assert image_hashes(tmp_path / "x.png") is not None


def test_unknown_sizes_skip_the_aspect_check(tmp_path: Path) -> None:
    image = scene(60)
    a = replace(record(tmp_path, image), width=None, height=None)
    b = record(tmp_path, image, quality=40)

    assert groups_of([a, b]) == [{a.id, b.id}]


def test_a_dhash_disagreement_alone_rejects_a_match(tmp_path: Path) -> None:
    image = scene(61)
    a = record(tmp_path, image)
    b = replace(
        record(tmp_path, image, quality=40), dhash=f"{int(a.dhash or '0', 16) ^ (2**64 - 1):016x}"
    )

    assert groups_of([a, b]) == []


# --- precision fixes from the real-sample pixel check (2026-10-20) --------------------------


def test_shots_one_second_apart_are_different_shots(tmp_path: Path) -> None:
    # Real sample: every true duplicate with EXIF on both sides shares the exact EXIF second;
    # the false positives were burst-style shots one second apart.
    first = record(tmp_path, scene(70), taken=T)
    second = record(tmp_path, scene(70, jitter=1), taken=T + timedelta(seconds=1))

    assert groups_of([first, second]) == []


def test_every_member_must_match_the_keeper_directly(tmp_path: Path) -> None:
    keeper = record(tmp_path, scene(71), key="a-keeper")
    link = replace(record(tmp_path, scene(72), key="b-link"), sha256=keeper.sha256)
    chained = replace(record(tmp_path, scene(73), key="c-chained"), content_id="CI")
    link = replace(link, content_id="CI")

    (group,) = find_duplicates([keeper, link, chained], DedupeConfig())

    assert set(group.members) == {keeper.id, link.id}, "c only matches b, not the keeper"
