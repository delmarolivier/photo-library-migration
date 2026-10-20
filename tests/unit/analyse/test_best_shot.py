"""Best shot: group similar shots taken close together, rank them, keep the top two."""

import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from photomigrate.analyse.best_shot import KEEP, Shot, exposure, group_shots, rank
from photomigrate.config import BestShotWeights
from photomigrate.faces import Face
from photomigrate.quality import PhotoMetrics
from photomigrate.testing.embedder import ThumbnailEmbedder
from photomigrate.testing.media import synthetic_image

T = datetime(2021, 6, 1, 12, 0, 0)
WINDOW = timedelta(minutes=2)
SIMILAR = 0.9
EMBEDDER = ThumbnailEmbedder()
_ids = iter(range(1, 100_000))


def metrics(sharpness: float = 200.0, brightness: float = 128.0) -> PhotoMetrics:
    return PhotoMetrics(
        sharpness=sharpness, brightness=brightness, dark_fraction=0.0, bright_fraction=0.0,
        contrast=50.0, megapixels=12.0, short_side=3024,
    )  # fmt: skip


def shot(
    seed: int = 1,
    *,
    at: float = 0,
    jitter: int = 0,
    image: Image.Image | None = None,
    sharpness: float = 200.0,
    brightness: float = 128.0,
    faces: tuple[Face, ...] | None = None,
    aesthetic: float | None = None,
    source: str = "google",
    key: str | None = None,
    pixels: int = 12_000_000,
) -> Shot:
    item_id = next(_ids)
    picture = image or synthetic_image(seed, (400, 300), jitter=jitter)
    return Shot(
        id=item_id,
        source=source,  # type: ignore[arg-type]
        key=key or f"shot-{item_id:05d}",
        taken=T + timedelta(seconds=at),
        embedding=EMBEDDER.embed(picture),
        metrics=metrics(sharpness, brightness),
        faces=faces,
        aesthetic=aesthetic,
        pixels=pixels,
    )


def keys(groups: list[list[Shot]]) -> list[list[str]]:
    return [[s.key for s in group] for group in groups]


def group(shots: list[Shot]) -> list[list[Shot]]:
    return group_shots(shots, window=WINDOW, min_similarity=SIMILAR)


# --- grouping ------------------------------------------------------------------------------


def test_a_burst_groups_together() -> None:
    burst = [shot(1, at=i * 0.3, jitter=i) for i in range(5)]

    assert keys(group(burst)) == [[s.key for s in burst]]


def test_the_same_scene_minutes_later_is_another_moment() -> None:
    first = [shot(1, at=0), shot(1, at=1, jitter=2)]
    later = [shot(1, at=300, jitter=3), shot(1, at=301, jitter=4)]

    assert keys(group(first + later)) == [[s.key for s in first], [s.key for s in later]]


def test_unrelated_shots_seconds_apart_do_not_group() -> None:
    assert group([shot(1, at=0), shot(2, at=5), shot(3, at=10)]) == []


def test_unrelated_shots_minutes_apart_do_not_group() -> None:
    assert group([shot(1, at=0), shot(2, at=180), shot(3, at=400)]) == []


def test_a_lone_shot_is_not_a_group() -> None:
    assert group([shot(1)]) == []


def test_two_subjects_taken_in_turn_make_two_groups() -> None:
    a1, b1, a2, b2 = shot(1, at=0), shot(2, at=2), shot(1, at=4, jitter=3), shot(2, at=6, jitter=3)

    assert keys(group([a1, b1, a2, b2])) == [[a1.key, a2.key], [b1.key, b2.key]]


def test_a_group_spans_at_most_the_window_from_its_first_shot() -> None:
    sequence = [shot(1, at=i * 50, jitter=i) for i in range(6)]  # 0, 50, … 250 s

    found = keys(group(sequence))

    assert found == [[s.key for s in sequence[:3]], [s.key for s in sequence[3:]]]


def test_a_shot_joins_the_group_it_is_most_like() -> None:
    # With the stand-in embedder: a~b 0.65, so they start two groups at 0.7;
    # x is 0.88 like a and 0.74 like b; b2 is b again.
    a, b = shot(1, at=0), shot(1, at=1, jitter=30)
    x, b2 = shot(1, at=2, jitter=20), shot(1, at=3, jitter=30)

    groups = group_shots([a, b, x, b2], window=WINDOW, min_similarity=0.7)

    assert keys(groups) == [[a.key, x.key], [b.key, b2.key]]


def test_the_threshold_decides_how_alike_is_alike() -> None:
    burst = [shot(1, at=0), shot(1, at=1, jitter=6)]

    assert group_shots(burst, window=WINDOW, min_similarity=-1.0) != []
    assert group_shots(burst, window=WINDOW, min_similarity=1.0) == []


def test_local_times_and_times_with_an_offset_compare_as_wall_clock() -> None:
    plain = shot(1, at=0)
    paris = replace(shot(1, jitter=2), taken=(T + timedelta(seconds=3)).replace(
        tzinfo=timezone(timedelta(hours=2))))  # fmt: skip

    assert keys(group([plain, paris])) == [[plain.key, paris.key]]


@given(st.randoms(use_true_random=False))
def test_grouping_does_not_depend_on_input_order(rng: random.Random) -> None:
    shots = BURSTS[:]
    rng.shuffle(shots)

    assert keys(group(shots)) == keys(group(BURSTS))


BURSTS = [
    *(shot(1, at=i, jitter=i) for i in range(4)),
    *(shot(2, at=2 + i, jitter=i) for i in range(3)),
    shot(3, at=500),
    *(shot(4, at=600 + i * 40, jitter=i) for i in range(5)),
]


# --- ranking -------------------------------------------------------------------------------

WEIGHTS = BestShotWeights()


def ranked(shots: list[Shot], weights: BestShotWeights = WEIGHTS) -> list[str]:
    return [r.shot.key for r in rank(shots, weights).shots]


def test_the_sharpest_shot_ranks_first() -> None:
    soft, sharp, middling = shot(sharpness=20), shot(sharpness=400), shot(sharpness=150)

    assert ranked([soft, sharp, middling]) == [sharp.key, middling.key, soft.key]


def test_exposure_is_best_at_mid_grey_and_worse_with_clipping() -> None:
    assert exposure(metrics(brightness=128)) == pytest.approx(1.0)
    assert exposure(metrics(brightness=64)) == pytest.approx(0.5)
    assert exposure(metrics(brightness=255)) == pytest.approx(0.0, abs=0.01)
    clipped = replace(metrics(), dark_fraction=0.2, bright_fraction=0.1)
    assert exposure(clipped) == pytest.approx(0.7)
    assert exposure(replace(metrics(brightness=10), dark_fraction=0.9)) == 0.0


def test_a_well_exposed_shot_beats_a_dark_one_of_equal_sharpness() -> None:
    dark, fine = shot(brightness=30), shot(brightness=120)

    assert ranked([dark, fine]) == [fine.key, dark.key]


def face(quality: float, eyes_open: bool | None = True) -> Face:
    return Face((0.4, 0.4, 0.2, 0.2), quality, eyes_open)


def test_with_faces_everywhere_open_eyes_and_good_faces_win() -> None:
    blink = shot(sharpness=400, faces=(face(0.7, eyes_open=False),))
    open_eyes = shot(sharpness=360, faces=(face(0.7),))
    weak_face = shot(sharpness=360, faces=(face(0.2),))

    ranking = rank([blink, open_eyes, weak_face], WEIGHTS)

    assert [r.shot.key for r in ranking.shots] == [open_eyes.key, weak_face.key, blink.key]
    assert {"faces", "eyes_open"} <= set(ranking.used)


def test_a_shot_without_a_face_scores_zero_for_faces_when_others_have_one() -> None:
    turned_away = shot(sharpness=400, faces=())
    looking = shot(sharpness=380, faces=(face(0.6),))

    assert ranked([turned_away, looking]) == [looking.key, turned_away.key]


def test_unknown_eyes_count_half() -> None:
    known = shot(faces=(face(0.5, eyes_open=True),))
    unknown = shot(faces=(face(0.5, eyes_open=None),))
    closed = shot(faces=(face(0.5, eyes_open=False),))

    ranking = rank([closed, unknown, known], WEIGHTS)

    parts = {r.shot.key: r.parts["eyes_open"] for r in ranking.shots}
    assert parts == {known.key: 1.0, unknown.key: 0.5, closed.key: 0.0}


def test_without_vision_ranking_falls_back_to_the_other_scores() -> None:
    """Linux CI: no face reader, so ``faces`` is ``None`` (not measured) everywhere."""
    soft = shot(sharpness=50, faces=None)
    sharp = shot(sharpness=300, faces=None)

    ranking = rank([soft, sharp], WEIGHTS)

    assert [r.shot.key for r in ranking.shots] == [sharp.key, soft.key]
    assert ranking.used == ("sharpness", "exposure")
    no_faces = BestShotWeights(faces=0, eyes_open=0)
    assert ranked([soft, sharp]) == ranked([soft, sharp], no_faces)


def test_faces_count_only_when_every_shot_was_read_for_them() -> None:
    read = shot(sharpness=100, faces=(face(0.9),))
    unread = shot(sharpness=300, faces=None)

    ranking = rank([read, unread], WEIGHTS)

    assert "faces" not in ranking.used
    assert [r.shot.key for r in ranking.shots] == [unread.key, read.key]


def test_faces_do_not_count_when_no_shot_has_one() -> None:
    assert "faces" not in rank([shot(faces=()), shot(faces=())], WEIGHTS).used


def test_apples_aesthetic_score_counts_when_every_shot_has_one() -> None:
    plain = shot(aesthetic=0.3, source="icloud")
    lovely = shot(aesthetic=0.9, source="icloud")
    google = shot(sharpness=300)

    assert ranked([plain, lovely]) == [lovely.key, plain.key]
    assert "aesthetic" not in rank([plain, lovely, google], WEIGHTS).used


def test_a_zero_weight_leaves_its_score_out() -> None:
    soft_bright, sharp_dark = shot(sharpness=50), shot(sharpness=300, brightness=40)

    assert ranked([soft_bright, sharp_dark], BestShotWeights(sharpness=0)) == [
        soft_bright.key,
        sharp_dark.key,
    ]
    assert "sharpness" not in rank([soft_bright], BestShotWeights(sharpness=0)).used


def test_scores_are_between_zero_and_one() -> None:
    ranking = rank([shot(sharpness=0.0, brightness=0), shot(sharpness=0.0)], WEIGHTS)

    assert all(0 <= r.score <= 1 for r in ranking.shots)
    assert all(0 <= value <= 1 for r in ranking.shots for value in r.parts.values())


def test_without_metrics_ties_go_to_resolution_then_icloud_then_the_key() -> None:
    small = replace(shot(key="a", pixels=100), metrics=None)
    google = replace(shot(key="b"), metrics=None)
    icloud = replace(shot(key="c", source="icloud"), metrics=None)

    ranking = rank([small, google, icloud], WEIGHTS)

    assert ranking.used == ()
    assert [r.shot.key for r in ranking.shots] == ["c", "b", "a"]


def test_exactly_the_top_two_are_kept() -> None:
    assert KEEP == 2
    shots = [shot(sharpness=s) for s in (10, 50, 400, 300, 20)]

    ranking = rank(shots, WEIGHTS)

    assert [r.shot.key for r in ranking.keep] == [shots[2].key, shots[3].key]
    assert {r.shot.key for r in ranking.rest} == {shots[0].key, shots[1].key, shots[4].key}


def test_a_pair_keeps_both() -> None:
    ranking = rank([shot(), shot(sharpness=1)], WEIGHTS)

    assert len(ranking.keep) == 2
    assert ranking.rest == []


@given(st.randoms(use_true_random=False))
def test_ranking_is_deterministic(rng: random.Random) -> None:
    shots = RANKED[:]
    rng.shuffle(shots)

    assert ranked(shots) == ranked(RANKED)


RANKED = [
    shot(sharpness=s, brightness=b, key=k)
    for k, s, b in [
        ("e", 100, 128),
        ("d", 100, 128),
        ("c", 300, 60),
        ("b", 50, 200),
        ("a", 300, 60),
    ]
]
