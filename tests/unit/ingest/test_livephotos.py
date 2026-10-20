"""Pairing Takeout's split Live Photos: content identifier first, then name and time."""

from datetime import UTC, datetime, timedelta

from hypothesis import assume, given
from hypothesis import strategies as st
from photomigrate.ingest.livephotos import Candidate, pair_live_photos

T = datetime(2019, 7, 5, 14, 20, 0, tzinfo=UTC)


def still(key: str, time: datetime | None = T, content_id: str | None = None) -> Candidate:
    return Candidate(key=key, time=time, content_id=content_id, duration=None)


def motion(
    key: str, time: datetime | None = T, content_id: str | None = None, duration: float = 1.5
) -> Candidate:
    return Candidate(key=key, time=time, content_id=content_id, duration=duration)


F = "Takeout/Google Photos/Photos from 2019"


def test_content_identifiers_pair_even_when_names_differ() -> None:
    result = pair_live_photos(
        [still(f"{F}/IMG_2001.HEIC", content_id="CI")],
        [motion(f"{F}/renamed.MOV", content_id="CI")],
    )

    assert [(p.still, p.motion, p.method) for p in result.pairs] == [
        (f"{F}/IMG_2001.HEIC", f"{F}/renamed.MOV", "content-id")
    ]
    assert result.unpaired == ()


def test_same_name_and_time_pair() -> None:
    result = pair_live_photos(
        [still(f"{F}/IMG_2001.HEIC", time=datetime(2019, 7, 5, 16, 20, 1))],  # naive local, UTC+2
        [motion(f"{F}/IMG_2001.MOV")],
    )

    (pair,) = result.pairs
    assert (pair.method, pair.delta) == ("name-time", 1.0)


def test_same_name_with_an_unknown_time_still_pairs() -> None:
    (pair,) = pair_live_photos(
        [still(f"{F}/IMG_2001.HEIC")], [motion(f"{F}/IMG_2001.MOV", time=None)]
    ).pairs

    assert (pair.method, pair.delta) == ("name", None)


def test_duplicate_indexes_are_part_of_the_name() -> None:
    result = pair_live_photos(
        [still(f"{F}/IMG_2001.HEIC"), still(f"{F}/IMG_2001(1).HEIC")],
        [motion(f"{F}/IMG_2001(1).MOV")],
    )

    assert [(p.still, p.motion) for p in result.pairs] == [
        (f"{F}/IMG_2001(1).HEIC", f"{F}/IMG_2001(1).MOV")
    ]


def test_same_name_but_far_apart_in_time_is_unpaired() -> None:
    result = pair_live_photos(
        [still(f"{F}/IMG_2001.HEIC")], [motion(f"{F}/IMG_2001.MOV", time=T + timedelta(seconds=10))]
    )

    assert result.pairs == ()
    assert result.unpaired == (f"{F}/IMG_2001.MOV",)


def test_a_motion_with_an_identifier_but_no_still_is_unpaired() -> None:
    result = pair_live_photos([], [motion(f"{F}/IMG_2001.MOV", content_id="CI")])

    assert result.unpaired == (f"{F}/IMG_2001.MOV",)


def test_short_clips_with_no_identifier_and_no_namesake_are_just_videos() -> None:
    result = pair_live_photos([still(f"{F}/IMG_1.HEIC")], [motion(f"{F}/IMG_1010.MOV")])

    assert result.pairs == ()
    assert result.unpaired == ()


def test_long_clips_are_never_motion_files() -> None:
    result = pair_live_photos(
        [still(f"{F}/IMG_2001.HEIC")], [motion(f"{F}/IMG_2001.MOV", duration=10.0, content_id="CI")]
    )

    assert result.pairs == ()
    assert result.unpaired == ()


def test_names_must_share_the_folder() -> None:
    result = pair_live_photos(
        [still(f"{F}/IMG_2001.HEIC")], [motion("Takeout/Google Photos/Holiday/IMG_2001.MOV")]
    )

    assert result.pairs == ()


def test_the_closest_motion_wins_and_each_file_pairs_once() -> None:
    result = pair_live_photos(
        [still(f"{F}/IMG_2001.HEIC")],
        [motion(f"{F}/IMG_2001.MOV", time=T + timedelta(seconds=2)), motion(f"{F}/IMG_2001.MP4")],
    )

    assert [p.motion for p in result.pairs] == [f"{F}/IMG_2001.MP4"]
    assert result.unpaired == (f"{F}/IMG_2001.MOV",)


def test_extensions_compare_without_case() -> None:
    (pair,) = pair_live_photos([still(f"{F}/IMG_2002.JPG")], [motion(f"{F}/img_2002.mp4")]).pairs

    assert pair.motion == f"{F}/img_2002.mp4"


_stems = st.integers(1000, 1030).map(lambda n: f"IMG_{n}")


@given(
    pairs=st.lists(
        st.tuples(_stems, st.integers(-3, 3), st.booleans()), max_size=8, unique_by=lambda p: p[0]
    ),
    loners=st.lists(st.tuples(_stems, st.sampled_from(["still", "clip"])), max_size=6),
)
def test_every_short_motion_with_a_matching_still_is_paired_with_it(
    pairs: list[tuple[str, int, bool]], loners: list[tuple[str, str]]
) -> None:
    taken = {stem for stem, _, _ in pairs}
    assume(all(stem not in taken for stem, _ in loners))
    stills = [
        still(f"{F}/{stem}.HEIC", content_id=f"CI-{stem}" if ci else None) for stem, _, ci in pairs
    ]
    motions = [
        motion(
            f"{F}/{stem}.MOV",
            time=T + timedelta(seconds=shift),
            content_id=f"CI-{stem}" if ci else None,
        )
        for stem, shift, ci in pairs
    ]
    stills += [still(f"{F}/{stem}-x.HEIC") for stem, kind in loners if kind == "still"]
    motions += [motion(f"{F}/{stem}-y.MOV") for stem, kind in loners if kind == "clip"]

    result = pair_live_photos(stills, motions)

    paired = {p.motion: p.still for p in result.pairs}
    for stem, _, _ in pairs:
        assert paired[f"{F}/{stem}.MOV"] == f"{F}/{stem}.HEIC"
    assert len(set(paired.values())) == len(paired)
    assert result.unpaired == ()
