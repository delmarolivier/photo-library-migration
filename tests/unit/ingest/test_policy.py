"""What ingest plans to write into working copies, and the date candidates it records."""

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from photomigrate.ingest import policy
from photomigrate.ingest.exif import Embedded
from photomigrate.ingest.policy import date_candidates, decide, local_zone
from photomigrate.ingest.sidecars import SidecarData

PARIS_SUMMER = timezone(timedelta(hours=2))
TAKEN_UTC = datetime(2019, 7, 1, 10, 34, 56, tzinfo=UTC)
UPLOADED = datetime(2019, 7, 3, 15, 0, tzinfo=UTC)
ZONE = ZoneInfo("Europe/Paris")
NICE = (43.6970333, 7.266, 12.0)


def sidecar(**changes: object) -> SidecarData:
    fields: dict[str, object] = {"title": "IMG_1001.JPG", "taken": TAKEN_UTC, "created": UPLOADED}
    fields.update(changes)
    return SidecarData(**fields)  # type: ignore[arg-type]


def test_a_missing_capture_date_is_filled_from_the_sidecar_in_the_home_zone() -> None:
    decision = decide(sidecar=sidecar(), embedded=Embedded(), zone=ZONE, writable=True)

    assert decision.plan.taken == TAKEN_UTC
    assert decision.plan.taken is not None
    assert decision.plan.taken.utcoffset() == timedelta(hours=2)
    assert {"date-from-sidecar", "offset-assumed"} <= decision.flags


def test_the_file_date_wins_and_agreeing_dates_raise_no_flag() -> None:
    embedded = Embedded(taken=TAKEN_UTC.astimezone(PARIS_SUMMER))

    decision = decide(sidecar=sidecar(), embedded=embedded, zone=ZONE, writable=True)

    assert decision.plan.taken is None
    assert "date-conflict" not in decision.flags


@pytest.mark.parametrize(
    ("embedded", "conflict"),
    [
        (datetime(2019, 7, 1, 12, 34, 56), False),  # naive local time, 2 h ahead of UTC
        (datetime(2019, 7, 1, 10, 34, 57), False),  # within the 2-second tolerance
        (datetime(2019, 7, 1, 12, 41, 0), True),  # not a whole-quarter-hour offset
        (datetime(2019, 7, 3, 12, 34, 56), True),  # more than 14 hours apart
        (datetime(2019, 7, 1, 12, 40, 0, tzinfo=PARIS_SUMMER), True),
    ],
)
def test_date_conflicts_allow_for_unknown_offsets(embedded: datetime, conflict: bool) -> None:
    decision = decide(
        sidecar=sidecar(), embedded=Embedded(taken=embedded), zone=ZONE, writable=True
    )

    assert ("date-conflict" in decision.flags) is conflict
    assert decision.plan.taken is None


@pytest.mark.parametrize(
    ("changes", "flag"),
    [
        ({"taken": datetime(1970, 1, 1, tzinfo=UTC)}, "sidecar-date-placeholder"),
        ({"taken": datetime(2000, 1, 1, 9, tzinfo=UTC)}, "sidecar-date-placeholder"),
        ({"taken": UPLOADED + timedelta(days=3)}, "sidecar-date-after-upload"),
        ({"taken": UPLOADED}, "sidecar-date-is-upload"),
    ],
)
def test_suspicious_sidecar_dates_are_never_written(changes: dict[str, object], flag: str) -> None:
    decision = decide(sidecar=sidecar(**changes), embedded=Embedded(), zone=ZONE, writable=True)

    assert decision.plan.taken is None
    assert {flag, "date-unknown"} <= decision.flags


def test_a_placeholder_file_date_is_replaced_by_a_good_sidecar_date() -> None:
    embedded = Embedded(taken=datetime(2000, 1, 1, 0, 3))

    decision = decide(sidecar=sidecar(), embedded=embedded, zone=ZONE, writable=True)

    assert decision.plan.taken == TAKEN_UTC
    assert {"embedded-date-placeholder", "date-from-sidecar"} <= decision.flags


def test_no_sidecar_and_no_file_date() -> None:
    decision = decide(sidecar=None, embedded=Embedded(), zone=ZONE, writable=True)

    assert not decision.plan
    assert {"no-sidecar", "date-unknown", "no-gps"} <= decision.flags


def test_gps_comes_from_the_sidecar_only_when_the_file_has_none() -> None:
    filled = decide(sidecar=sidecar(geo=NICE), embedded=Embedded(), zone=ZONE, writable=True)
    kept = decide(
        sidecar=sidecar(geo=NICE),
        embedded=Embedded(latitude=1.0, longitude=2.0),
        zone=ZONE,
        writable=True,
    )

    assert (filled.plan.latitude, filled.plan.longitude, filled.plan.altitude) == NICE
    assert "gps-from-sidecar" in filled.flags
    assert kept.plan.latitude is None
    assert "gps-from-sidecar" not in kept.flags


def test_the_sidecar_caption_is_written_when_it_differs() -> None:
    new = decide(
        sidecar=sidecar(description="Plage à Nice 🏖️"),
        embedded=Embedded(description="OLYMPUS DIGITAL CAMERA"),
        zone=ZONE,
        writable=True,
    )
    same = decide(
        sidecar=sidecar(description="Plage"),
        embedded=Embedded(description="Plage"),
        zone=ZONE,
        writable=True,
    )

    assert new.plan.description == "Plage à Nice 🏖️"
    assert "description-from-sidecar" in new.flags
    assert same.plan.description is None


def test_plans_for_unwritable_formats_are_kept_and_flagged() -> None:
    decision = decide(sidecar=sidecar(), embedded=Embedded(), zone=ZONE, writable=False)

    assert decision.plan.taken == TAKEN_UTC
    assert "metadata-not-writable" in decision.flags


def test_every_date_candidate_is_collected() -> None:
    found = date_candidates(
        kind="photo",
        embedded=Embedded(taken=datetime(2012, 3, 4, 10, 11, 12)),
        sidecar=sidecar(),
        name="IMG_20120304_101112.jpg",
        modified=datetime(2026, 10, 20, 8, 42, 20),
    )

    assert found == {
        "exif": datetime(2012, 3, 4, 10, 11, 12),
        "sidecar-taken": TAKEN_UTC,
        "sidecar-created": UPLOADED,
        "filename": datetime(2012, 3, 4, 10, 11, 12),
        "mtime": datetime(2026, 10, 20, 8, 42, 20),
    }


def test_video_candidates_use_quicktime_and_skip_what_is_missing() -> None:
    found = date_candidates(
        kind="video",
        embedded=Embedded(taken=TAKEN_UTC),
        sidecar=None,
        name="IMG_1010.MOV",
        modified=datetime(2026, 10, 20, 8, 42, 20),
    )

    assert found == {"quicktime": TAKEN_UTC, "mtime": datetime(2026, 10, 20, 8, 42, 20)}


def test_local_zone_prefers_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")

    assert local_zone() == ZoneInfo("Asia/Tokyo")


def test_local_zone_reads_the_localtime_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        policy, "_localtime_target", lambda: "/var/db/timezone/zoneinfo/Europe/London"
    )

    assert local_zone() == ZoneInfo("Europe/London")


@pytest.mark.parametrize("target", [None, "/etc/garbage", "/usr/share/zoneinfo/Not/AZone"])
def test_local_zone_falls_back_to_utc(monkeypatch: pytest.MonkeyPatch, target: str | None) -> None:
    monkeypatch.setenv("TZ", "Not/AZone")
    monkeypatch.setattr(policy, "_localtime_target", lambda: target)

    assert local_zone() == ZoneInfo("UTC")


def test_a_sidecar_without_an_upload_time_still_counts() -> None:
    found = date_candidates(
        kind="photo",
        embedded=Embedded(),
        sidecar=sidecar(created=None),
        name="IMG_1001.JPG",
        modified=datetime(2026, 10, 20, 8, 42, 20),
    )

    assert "sidecar-created" not in found
    assert found["sidecar-taken"] == TAKEN_UTC


def test_the_localtime_link_is_read_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_path: str) -> str:
        raise OSError("no link")

    assert policy._localtime_target() is None or "zoneinfo" in str(policy._localtime_target())
    monkeypatch.setattr(policy.os, "readlink", broken)
    assert policy._localtime_target() is None
