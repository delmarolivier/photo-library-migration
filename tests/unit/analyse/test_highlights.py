"""Highlights: keep the best share of each year's photos, spread across its events."""

from dataclasses import replace
from datetime import datetime, timedelta

from photomigrate.analyse.best_shot import Shot
from photomigrate.analyse.highlights import (
    BAD,
    GOOD,
    Appeal,
    Candidate,
    allocate,
    events,
    quota,
    select_year,
)
from photomigrate.config import BestShotWeights
from photomigrate.quality import PhotoMetrics
from photomigrate.testing.embedder import ThumbnailEmbedder
from photomigrate.testing.media import synthetic_image

T = datetime(2014, 3, 1, 9, 0)
GAP = timedelta(hours=6)
EMBEDDER = ThumbnailEmbedder()
_ids = iter(range(1, 1_000_000))


def shot(seed: int, *, at: timedelta = timedelta(), sharpness: float = 200.0) -> Shot:
    item_id = next(_ids)
    return Shot(
        id=item_id,
        source="google",
        key=f"shot-{item_id:06d}",
        taken=T + at,
        embedding=EMBEDDER.embed(synthetic_image(seed, (64, 48))),
        metrics=PhotoMetrics(
            sharpness=sharpness,
            brightness=128.0,
            dark_fraction=0.0,
            bright_fraction=0.0,
            contrast=50.0,
            megapixels=12.0,
            short_side=3024,
        ),
        faces=None,
        aesthetic=None,
        pixels=12_000_000,
    )


def free(*shots: Shot) -> list[Candidate]:
    return [Candidate(s) for s in shots]


def select(
    candidates: list[Candidate],
    *,
    share: float = 0.2,
    appeal: dict[int, float] | None = None,
    max_similarity: float = 0.999,
) -> list[tuple[list[int], set[int]]]:
    """Each event as (its shot ids in time order, the ids kept)."""
    found = select_year(
        candidates,
        share=share,
        gap=GAP,
        weights=BestShotWeights(),
        appeal=(lambda s: (appeal or {}).get(s.id, 0.5)),
        appeal_weight=0.4,
        max_similarity=max_similarity,
    )
    return [([s.id for s in e.shots], set(e.keep)) for e in found]


# --- quota and allocation --------------------------------------------------------------------


def test_the_quota_is_the_share_rounded_up() -> None:
    assert quota(100, 0.2) == 20
    assert quota(101, 0.2) == 21
    assert quota(3, 0.2) == 1
    assert quota(0, 0.2) == 0


def test_places_go_to_events_in_proportion_to_their_size() -> None:
    assert allocate([50, 30, 20], 10) == [5, 3, 2]


def test_left_over_places_go_to_the_largest_remainders() -> None:
    assert allocate([7, 7, 6], 4) == [2, 1, 1]


def test_an_event_never_gets_more_places_than_it_has_shots_and_small_ones_may_get_none() -> None:
    assert allocate([1, 99], 10) == [0, 10]
    assert allocate([2, 3], 10) == [2, 3]


def test_no_places_means_none_for_anyone() -> None:
    assert allocate([5, 5], 0) == [0, 0]


# --- events ----------------------------------------------------------------------------------


def test_shots_more_than_the_gap_apart_are_separate_events() -> None:
    a = shot(1)
    b = shot(2, at=timedelta(hours=2))
    c = shot(3, at=timedelta(hours=9))

    assert [[s.id for s in e] for e in events([c, a, b], GAP)] == [[a.id, b.id], [c.id]]


# --- selection -------------------------------------------------------------------------------


def test_a_year_keeps_its_share_and_sets_the_rest_aside() -> None:
    shots = [shot(n, at=timedelta(minutes=n)) for n in range(1, 11)]

    [(ids, kept)] = select(free(*shots))

    assert ids == [s.id for s in shots]
    assert len(kept) == 2


def test_the_sharpest_and_most_appealing_shots_are_kept() -> None:
    shots = [shot(n, at=timedelta(minutes=n), sharpness=100.0) for n in range(1, 11)]
    shots[3] = replace(shots[3], metrics=replace(shots[3].metrics, sharpness=400.0))  # type: ignore[type-var]
    appealing = shots[7]

    [(_, kept)] = select(free(*shots), appeal={appealing.id: 1.0})

    assert kept == {shots[3].id, appealing.id}


def test_every_event_gets_its_part_of_the_year() -> None:
    morning = [shot(n, at=timedelta(minutes=n)) for n in range(1, 11)]
    trip = [shot(20 + n, at=timedelta(days=30, minutes=n)) for n in range(1, 21)]

    found = select(free(*morning, *trip), share=0.1)

    assert [len(kept) for _, kept in found] == [1, 2]


def test_favourites_are_always_kept_and_count_towards_the_share() -> None:
    shots = [shot(n, at=timedelta(minutes=n), sharpness=400.0) for n in range(1, 11)]
    starred = shot(50, at=timedelta(minutes=20), sharpness=10.0)
    also = shot(51, at=timedelta(minutes=21), sharpness=10.0)
    candidates = [
        *free(*shots),
        Candidate(starred, protected="favourite"),
        Candidate(also, protected="favourite"),
    ]  # 12 photos: a share of 3

    [(_, kept)] = select(candidates)

    assert {starred.id, also.id} <= kept
    assert len(kept) == 3


def test_protected_photos_beyond_the_share_are_all_kept() -> None:
    starred = [shot(n, at=timedelta(minutes=n)) for n in range(1, 6)]
    other = [shot(10 + n, at=timedelta(minutes=10 + n)) for n in range(1, 6)]

    [(_, kept)] = select([*(Candidate(s, protected="favourite") for s in starred), *free(*other)])

    assert kept == {s.id for s in starred}


def test_a_near_repeat_of_a_kept_shot_gives_way_to_a_different_one() -> None:
    first = shot(7, at=timedelta(minutes=1), sharpness=400.0)
    again = replace(shot(8, at=timedelta(minutes=2), sharpness=390.0), embedding=first.embedding)
    others = [shot(30 + n, at=timedelta(minutes=10 + n), sharpness=100.0) for n in range(8)]

    [(_, kept)] = select(free(first, again, *others), max_similarity=0.95)

    assert first.id in kept
    assert again.id not in kept
    assert len(kept) == 2


def test_near_repeats_fill_the_places_when_nothing_else_is_left() -> None:
    first = shot(7, at=timedelta(minutes=1))
    copies = [
        replace(shot(8 + n, at=timedelta(minutes=2 + n)), embedding=first.embedding)
        for n in range(4)
    ]

    [(_, kept)] = select(free(first, *copies), share=0.4, max_similarity=0.95)

    assert len(kept) == 2


def test_selection_is_deterministic() -> None:
    shots = [shot(n, at=timedelta(minutes=n)) for n in range(1, 31)]

    assert select(free(*shots)) == select(free(*reversed(shots)))


# --- appeal --------------------------------------------------------------------------------


def test_appeal_is_high_for_a_picture_like_a_good_photo_and_low_for_a_poor_one() -> None:
    appeal = Appeal(EMBEDDER)
    (good,) = EMBEDDER.embed_texts([GOOD[0]])
    (bad,) = EMBEDDER.embed_texts([BAD[0]])

    assert 0.5 < appeal.score(good) <= 1
    assert 0 <= appeal.score(bad) < 0.5


def test_appeal_reads_the_text_prompts_once() -> None:
    calls: list[int] = []

    class Counting(ThumbnailEmbedder):
        def embed_texts(self, texts):  # type: ignore[no-untyped-def]
            calls.append(len(texts))
            return super().embed_texts(texts)

    appeal = Appeal(Counting())
    (vector,) = EMBEDDER.embed_texts(["anything"])
    appeal.score(vector)
    appeal.score(vector)

    assert calls == [len(GOOD) + len(BAD)]
