"""Google Maps Timeline (iOS on-device export): where the phone was at a given moment."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from photomigrate.testing.timeline import TimelineBuilder
from photomigrate.timeline import Place, TimelineError, load_timeline, parse_geo, parse_timeline

PARIS = timezone(timedelta(hours=2))
T = datetime(2025, 6, 1, 10, 0, tzinfo=PARIS)
TOLERANCE = timedelta(minutes=15)


def at(minutes: float) -> datetime:
    return T + timedelta(minutes=minutes)


def test_geo_strings() -> None:
    assert parse_geo("geo:48.856600,2.352200") == (48.8566, 2.3522)
    assert parse_geo("geo:-33.8568,151.2153") == (-33.8568, 151.2153)
    with pytest.raises(TimelineError, match="not a geo"):
        parse_geo("48.8,2.3")


def test_a_moment_during_a_visit_is_at_the_visited_place() -> None:
    timeline = parse_timeline(TimelineBuilder().visit(at(0), at(60), 48.85, 2.35).data())

    assert timeline.locate(at(30), TOLERANCE) == Place(48.85, 2.35, "visit")


def test_the_shortest_of_nested_visits_is_the_most_precise() -> None:
    data = TimelineBuilder().visit(at(0), at(240), 48.0, 2.0).visit(at(60), at(90), 48.5, 2.5)

    assert parse_timeline(data.data()).locate(at(75), TOLERANCE) == Place(48.5, 2.5, "visit")


def test_between_two_path_points_the_place_is_interpolated() -> None:
    data = TimelineBuilder().path(at(0), at(20), [(0, 48.0, 2.0), (20, 48.2, 2.4)])

    found = parse_timeline(data.data()).locate(at(5), TOLERANCE)

    assert found == Place(pytest.approx(48.05), pytest.approx(2.1), "path")  # type: ignore[arg-type]


def test_activities_add_their_start_and_end_as_points() -> None:
    data = TimelineBuilder().activity(at(0), at(10), (48.0, 2.0), (48.1, 2.2))

    timeline = parse_timeline(data.data())

    assert timeline.locate(at(0), TOLERANCE) == Place(48.0, 2.0, "path")
    assert timeline.locate(at(12), TOLERANCE) == Place(48.1, 2.2, "path")


def test_a_lone_point_within_the_tolerance_is_used_as_it_is() -> None:
    data = TimelineBuilder().path(at(0), at(0), [(0, 48.0, 2.0)])

    timeline = parse_timeline(data.data())

    assert timeline.locate(at(10), TOLERANCE) == Place(48.0, 2.0, "path")
    assert timeline.locate(at(-10), TOLERANCE) == Place(48.0, 2.0, "path")


def test_nothing_is_known_far_from_every_point() -> None:
    data = TimelineBuilder().path(at(0), at(0), [(0, 48.0, 2.0)]).visit(at(60), at(70), 1, 1)

    timeline = parse_timeline(data.data())

    assert timeline.locate(at(30), TOLERANCE) is None
    assert timeline.locate(at(-60), TOLERANCE) is None


def test_points_far_apart_are_not_interpolated_across_a_gap() -> None:
    data = TimelineBuilder().path(at(0), at(120), [(0, 48.0, 2.0), (120, 49.0, 3.0)])

    timeline = parse_timeline(data.data())

    assert timeline.locate(at(60), TOLERANCE) is None
    assert timeline.locate(at(5), TOLERANCE) == Place(48.0, 2.0, "path")


def test_times_in_another_zone_are_the_same_moments() -> None:
    timeline = parse_timeline(TimelineBuilder().visit(at(0), at(60), 48.85, 2.35).data())

    assert timeline.locate(at(30).astimezone(UTC), TOLERANCE) is not None


def test_a_moment_without_an_offset_cannot_be_placed() -> None:
    timeline = parse_timeline(TimelineBuilder().visit(at(0), at(60), 48.85, 2.35).data())

    with pytest.raises(ValueError, match="time zone"):
        timeline.locate(datetime(2025, 6, 1, 10, 30), TOLERANCE)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"semanticSegments": []}, "a list of segments"),
        ([{"startTime": "soon", "endTime": "later", "visit": {}}], "segment 0"),
        ([{"visit": {}}], "segment 0"),
        (
            [{"startTime": "2025-06-01T10:00:00+02:00", "endTime": "2025-06-01T11:00:00+02:00",
              "visit": {"topCandidate": {"placeLocation": "nowhere"}}}],
            "segment 0",
        ),
    ],
)  # fmt: skip
def test_a_broken_export_is_an_error_naming_the_segment(data: object, message: str) -> None:
    with pytest.raises(TimelineError, match=message):
        parse_timeline(data)


def test_segments_of_other_kinds_are_ignored() -> None:
    data = [{"startTime": "2025-06-01T10:00:00+02:00", "endTime": "2025-06-01T11:00:00+02:00",
             "timelineMemory": {}}]  # fmt: skip

    assert parse_timeline(data).locate(at(30), TOLERANCE) is None


def test_loading_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "Timeline.json"
    path.write_text(TimelineBuilder().visit(at(0), at(60), 48.85, 2.35).to_json(), "utf-8")

    assert load_timeline(path).locate(at(1), TOLERANCE) is not None
    with pytest.raises(TimelineError, match="no Timeline export"):
        load_timeline(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{", "utf-8")
    with pytest.raises(TimelineError, match="not JSON"):
        load_timeline(tmp_path / "bad.json")


def test_the_builder_writes_the_ios_format_with_strings() -> None:
    data = json.loads(
        TimelineBuilder()
        .visit(at(0), at(60), 48.85, 2.35)
        .activity(at(60), at(70), (48.85, 2.35), (48.9, 2.4))
        .path(at(60), at(70), [(5, 48.87, 2.37)])
        .to_json()
    )

    visit, activity, path = data
    assert visit["visit"]["topCandidate"]["placeLocation"] == "geo:48.850000,2.350000"
    assert visit["startTime"] == "2025-06-01T10:00:00.000+02:00"
    assert activity["activity"]["start"] == "geo:48.850000,2.350000"
    assert path["timelinePath"] == [
        {"point": "geo:48.870000,2.370000", "durationMinutesOffsetFromStartTime": "5"}
    ]
