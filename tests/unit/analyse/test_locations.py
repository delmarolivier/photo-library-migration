"""Location backfill: borrow GPS from the same camera's shots nearby in time, else Timeline."""

from datetime import datetime, timedelta, timezone

import pytest

from photomigrate.analyse.locations import Inferred, Spot, infer
from photomigrate.testing.timeline import TimelineBuilder
from photomigrate.timeline import parse_timeline

T = datetime(2025, 6, 1, 12, 0)
WINDOW = timedelta(minutes=30)
TOLERANCE = timedelta(minutes=15)
PARIS = timezone(timedelta(hours=2))
_ids = iter(range(1, 100_000))


def spot(
    minutes: float | None,
    at: tuple[float, float] | None = None,
    *,
    camera: str | None = "Apple iPhone 12",
) -> Spot:
    item_id = next(_ids)
    latitude, longitude = at if at else (None, None)
    return Spot(
        id=item_id,
        key=f"item-{item_id}",
        camera=camera,
        taken=None if minutes is None else T + timedelta(minutes=minutes),
        latitude=latitude,
        longitude=longitude,
    )


def run(spots: list[Spot], **options: object) -> dict[int, Inferred]:
    return infer(spots, window=WINDOW, **options)  # type: ignore[arg-type]


def test_a_shot_borrows_the_location_of_the_same_cameras_shot_minutes_before() -> None:
    located, lost = spot(0, (48.85, 2.35)), spot(10)

    found = run([located, lost])

    assert found == {
        lost.id: Inferred(48.85, 2.35, "neighbour", {"from": [located.key], "gaps": [600]})
    }


def test_or_minutes_after() -> None:
    lost, located = spot(0), spot(20, (48.85, 2.35))

    assert run([lost, located])[lost.id].method == "neighbour"


def test_between_two_located_shots_the_location_is_interpolated_in_time() -> None:
    first, lost, last = spot(0, (48.0, 2.0)), spot(10), spot(20, (48.2, 2.4))

    found = run([first, lost, last])[lost.id]

    assert (found.latitude, found.longitude) == (pytest.approx(48.1), pytest.approx(2.2))
    assert found.method == "interpolated"
    assert found.evidence == {"from": [first.key, last.key], "gaps": [600, 600]}


def test_the_interpolation_follows_the_time_not_the_middle() -> None:
    first, lost, last = spot(0, (48.0, 2.0)), spot(5), spot(20, (48.2, 2.4))

    found = run([first, lost, last])[lost.id]

    assert found.latitude == pytest.approx(48.05)
    assert found.longitude == pytest.approx(2.1)


def test_the_nearest_shot_on_each_side_counts() -> None:
    far, near, lost = spot(-25, (40.0, 0.0)), spot(-5, (48.0, 2.0)), spot(0)

    assert run([far, near, lost])[lost.id].evidence["from"] == [near.key]


def test_nothing_is_inferred_without_a_neighbour_in_the_window() -> None:
    before, lost, after = spot(-31, (48.0, 2.0)), spot(0), spot(31, (48.2, 2.4))

    assert run([before, lost, after]) == {}


def test_only_the_same_camera_counts() -> None:
    other, lost = spot(0, (48.0, 2.0), camera="Canon EOS 5D"), spot(5)

    assert run([other, lost]) == {}


def test_items_without_a_camera_or_a_date_are_left_alone() -> None:
    located = spot(0, (48.0, 2.0))

    assert run([located, spot(1, camera=None), spot(None)]) == {}


def test_located_items_keep_their_own_location() -> None:
    a, b = spot(0, (48.0, 2.0)), spot(5, (49.0, 3.0))

    assert run([a, b]) == {}


def test_local_times_and_times_with_offsets_compare_as_wall_clock() -> None:
    located = spot(0, (48.0, 2.0))
    lost = Spot(id=next(_ids), key="aware", camera="Apple iPhone 12",
                taken=(T + timedelta(minutes=5)).replace(tzinfo=PARIS), latitude=None,
                longitude=None)  # fmt: skip

    assert run([located, lost])[lost.id].method == "neighbour"


# --- Timeline ------------------------------------------------------------------------------


def timeline() -> object:
    start = T.replace(tzinfo=PARIS)
    return parse_timeline(
        TimelineBuilder()
        .visit(start, start + timedelta(hours=1), 48.85, 2.35)
        .path(
            start + timedelta(hours=2),
            start + timedelta(hours=3),
            [(0, 48.0, 2.0), (10, 48.1, 2.1)],
        )
        .data()
    )


def test_without_neighbours_the_timeline_says_where_the_phone_was() -> None:
    in_visit, on_path, unknown = spot(30), spot(125), spot(300)

    found = run([in_visit, on_path, unknown], timeline=timeline(), zone=PARIS, tolerance=TOLERANCE)

    assert found[in_visit.id] == Inferred(48.85, 2.35, "timeline-visit", {})
    assert found[on_path.id].method == "timeline-path"
    assert found[on_path.id].latitude == pytest.approx(48.05)
    assert unknown.id not in found


def test_a_neighbour_beats_the_timeline() -> None:
    located, lost = spot(25, (40.0, 0.0)), spot(30)

    found = run([located, lost], timeline=timeline(), zone=PARIS, tolerance=TOLERANCE)

    assert found[lost.id].method == "neighbour"


def test_the_timeline_needs_a_zone_for_local_times() -> None:
    with pytest.raises(ValueError, match="zone"):
        run([spot(30)], timeline=timeline(), tolerance=TOLERANCE)
