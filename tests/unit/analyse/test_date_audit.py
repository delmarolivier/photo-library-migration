"""Date audit: why an item's date is doubtful, its batch, and a suggested date."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from photomigrate.analyse.date_audit import (
    Dated,
    audit,
    batches,
    item_reasons,
    out_of_sequence,
    sequence_of,
    suggestions,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
_ids = iter(range(1, 100_000))


def item(
    name: str = "IMG_0001.JPG",
    *,
    taken: datetime | None = datetime(2019, 7, 1, 12, 0),
    camera: str | None = "Apple iPhone 8",
    folder: str = "Photos from 2019",
    source: str = "google",
    **candidates: datetime,
) -> Dated:
    found = {k.replace("_", "-"): v for k, v in candidates.items()}
    if taken is not None and "exif" not in found and "icloud" not in found:
        found["exif"] = taken
    return Dated(
        id=next(_ids),
        source=source,  # type: ignore[arg-type]
        key=f"{folder}/{name}",
        name=name,
        folder=folder,
        camera=camera,
        taken=taken,
        candidates=found,
    )


# --- each reason ---------------------------------------------------------------------------


def test_a_good_date_raises_nothing() -> None:
    assert item_reasons(item(), NOW) == set()


def test_a_missing_date() -> None:
    assert item_reasons(item(taken=None), NOW) == {"missing"}


@pytest.mark.parametrize("day", [datetime(1970, 1, 1), datetime(1980, 1, 1), datetime(2000, 1, 1)])
def test_a_placeholder_in_the_file_is_flagged_even_when_a_sidecar_filled_the_gap(
    day: datetime,
) -> None:
    filled = item(taken=datetime(2019, 7, 1, 12, 0, tzinfo=UTC), exif=day)

    assert "placeholder" in item_reasons(filled, NOW)
    assert "placeholder" in item_reasons(item(taken=None, icloud=day), NOW)


def test_a_placeholder_sidecar_counts_only_without_a_file_date() -> None:
    undated = item(taken=None, sidecar_taken=datetime(1970, 1, 1, tzinfo=UTC))
    dated = item(sidecar_taken=datetime(1970, 1, 1, tzinfo=UTC))

    assert item_reasons(undated, NOW) == {"missing", "placeholder"}
    assert item_reasons(dated, NOW) == set()


def test_a_date_in_the_future() -> None:
    assert item_reasons(item(taken=datetime(2031, 1, 1)), NOW) == {"future"}
    assert item_reasons(item(taken=datetime(2026, 10, 20, 23, 0)), NOW) == set(), (
        "a local time later today is not the future"
    )


def test_the_upload_time_used_as_the_capture_time() -> None:
    upload = datetime(2020, 5, 5, 10, 0, tzinfo=UTC)
    guessed = item(taken=None, sidecar_taken=upload, sidecar_created=upload + timedelta(seconds=1))

    assert item_reasons(guessed, NOW) == {"missing", "upload-as-taken"}
    with_exif = item(sidecar_taken=upload, sidecar_created=upload)
    assert "upload-as-taken" not in item_reasons(with_exif, NOW)


def test_sources_more_than_a_day_apart_conflict() -> None:
    taken = datetime(2019, 7, 1, 12, 0)

    assert item_reasons(item(taken=taken, sidecar_taken=datetime(2019, 9, 1, tzinfo=UTC)), NOW) == {
        "conflict"
    }
    assert item_reasons(item(taken=taken, filename=datetime(2019, 7, 3)), NOW) == {"conflict"}
    near = item(taken=taken, sidecar_taken=datetime(2019, 7, 1, 20, 0, tzinfo=UTC))
    assert item_reasons(near, NOW) == set(), "under a day apart is not a conflict"


def test_a_local_time_and_an_instant_a_whole_offset_apart_agree() -> None:
    paris = timezone(timedelta(hours=2))
    local = item(
        taken=datetime(2019, 7, 1, 12, 0), sidecar_taken=datetime(2019, 7, 1, 12, 0, tzinfo=paris)
    )

    assert item_reasons(local, NOW) == set()


def test_the_upload_time_and_mtime_never_conflict() -> None:
    late = item(
        sidecar_created=datetime(2024, 1, 1, tzinfo=UTC), mtime=datetime(2026, 10, 20, 8, 0)
    )

    assert item_reasons(late, NOW) == set()


def test_an_answered_date_is_never_flagged_again() -> None:
    answered = item(taken=None, user=datetime(2004, 7, 1, 12, 0))

    assert item_reasons(answered, NOW) == set()


# --- sequences -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("IMG_1234.JPG", ("IMG_", 1234)),
        ("IMG_1234(1).JPG", ("IMG_", 1234)),
        ("IMG_1234-edited.JPG", ("IMG_", 1234)),
        ("DSC01234.JPG", ("DSC", 1234)),
        ("P1010001.JPG", ("P", 1010001)),
        ("IMG-20190702-WA0003.jpg", ("IMG-", 20190702)),
        ("holiday.jpg", None),
        ("2019-07-01 12.34.56.jpg", ("", 2019)),
    ],
)
def test_sequence_of_a_name(name: str, expected: tuple[str, int] | None) -> None:
    assert sequence_of(name) == expected


def run(*days: int | None, camera: str = "Apple iPhone 8") -> list[Dated]:
    """IMG_0001… with the given day of July 2019 (None: undated)."""
    return [
        item(
            f"IMG_{n:04d}.JPG",
            taken=None if day is None else datetime(2019, 7, day, 12, 0),
            camera=camera,
        )
        for n, day in enumerate(days, start=1)
    ]


def test_a_shot_dated_far_from_its_camera_neighbours_is_out_of_sequence() -> None:
    shots = run(1, 2, 3, 4, 5)
    stray = item("IMG_0003.JPG", taken=datetime(2015, 1, 1, 12, 0))
    shots[2] = stray

    assert out_of_sequence(shots) == {stray.id}


def test_neighbours_that_disagree_with_each_other_flag_nothing() -> None:
    assert out_of_sequence(run(10, 5, 2)) == set()


def test_a_shot_at_either_end_has_one_neighbour_and_is_not_judged() -> None:
    assert out_of_sequence(run(28, 2, 3)) == set()


def test_other_cameras_are_other_sequences() -> None:
    mine = run(1, 2)
    other = item("IMG_0002.JPG", taken=datetime(2015, 1, 1), camera="Canon EOS 5D")
    later = item("IMG_0003.JPG", taken=datetime(2019, 7, 3, 12, 0))

    assert out_of_sequence([*mine, other, later]) == set()


def test_audit_lists_every_flagged_item_with_its_reasons() -> None:
    shots = run(1, 2, None, 4, 5)
    stray = item("IMG_0006.JPG", taken=datetime(2015, 1, 1, 12, 0))
    end = item("IMG_0007.JPG", taken=datetime(2019, 7, 7, 12, 0))

    found = audit([*shots, stray, end], NOW)

    assert found == {shots[2].id: {"missing"}, stray.id: {"out-of-sequence"}}


# --- batches and suggestions ---------------------------------------------------------------


def test_flagged_items_are_batched_by_problem_camera_folder_and_prefix() -> None:
    a = item("IMG_0001.JPG", taken=None)
    b = item("IMG_0002.JPG", taken=None)
    c = item("DSC0001.JPG", taken=None)
    d = item("IMG_0003.JPG", taken=None, folder="Holiday")
    e = item("IMG_0004.JPG", taken=None, camera="Canon EOS 5D")
    f = item("IMG_0005.JPG", taken=datetime(2031, 1, 1))
    found = audit([a, b, c, d, e, f], NOW)

    groups = batches([a, b, c, d, e, f], found)

    assert sorted([m.name for m in batch.members] for batch in groups) == [
        ["DSC0001.JPG"],
        ["IMG_0001.JPG", "IMG_0002.JPG"],
        ["IMG_0003.JPG"],
        ["IMG_0004.JPG"],
        ["IMG_0005.JPG"],
    ]
    undated = next(batch for batch in groups if len(batch.members) == 2)
    assert (undated.problem, undated.camera, undated.folder, undated.prefix) == (
        "undated", "Apple iPhone 8", "Photos from 2019", "IMG_",
    )  # fmt: skip
    assert next(batch for batch in groups if batch.members == (f,)).problem == "doubtful"


def test_a_batch_key_names_the_batch_not_its_members() -> None:
    a, b = item("IMG_0001.JPG", taken=None), item("IMG_0002.JPG", taken=None)

    (alone,) = batches([a], audit([a], NOW))
    (both,) = batches([a, b], audit([a, b], NOW))

    assert alone.key == both.key
    assert alone.key.startswith("dates-")


def test_members_are_in_sequence_order() -> None:
    shots = [item(f"IMG_{n:04d}.JPG", taken=None) for n in (10, 9, 100, 2)]

    (batch,) = batches(shots, audit(shots, NOW))

    assert [m.name for m in batch.members] == [
        "IMG_0002.JPG", "IMG_0009.JPG", "IMG_0010.JPG", "IMG_0100.JPG",
    ]  # fmt: skip


def test_a_suggestion_interpolates_between_well_dated_neighbours() -> None:
    shots = run(1, None, None, 4)

    found = suggestions(shots, audit(shots, NOW))

    assert found[shots[1].id] == datetime(2019, 7, 2, 12, 0)
    assert found[shots[2].id] == datetime(2019, 7, 3, 12, 0)


def test_a_suggestion_from_one_side_takes_that_neighbours_date() -> None:
    shots = run(None, 2, 3)

    assert suggestions(shots, audit(shots, NOW))[shots[0].id] == datetime(2019, 7, 2, 12, 0)


def test_flagged_neighbours_are_never_the_source_of_a_suggestion() -> None:
    shots = run(1, 2, 3, 4, 5)
    shots[1] = item("IMG_0002.JPG", taken=datetime(2031, 1, 1))
    shots[2] = item("IMG_0003.JPG", taken=None)

    found = suggestions(shots, audit(shots, NOW))

    assert found[shots[2].id] == datetime(2019, 7, 3, 12, 0)


def test_a_date_in_the_name_is_the_best_suggestion() -> None:
    whatsapp = item(
        "IMG-20190702-WA0003.jpg", taken=None, camera=None, filename=datetime(2019, 7, 2)
    )

    assert suggestions([whatsapp], audit([whatsapp], NOW))[whatsapp.id] == datetime(2019, 7, 2)


def test_a_plausible_sidecar_date_is_the_last_resort() -> None:
    sidecar = datetime(2019, 7, 2, 9, 0, tzinfo=UTC)
    conflicting = item(
        "holiday.jpg", camera=None, sidecar_taken=sidecar, filename=datetime(2019, 9, 9)
    )
    upload = item(
        "beach.jpg", taken=None, camera=None, sidecar_taken=sidecar, sidecar_created=sidecar
    )
    lost = item("sea.jpg", taken=None, camera=None)

    found = suggestions([conflicting, upload, lost], audit([conflicting, upload, lost], NOW))

    assert found[conflicting.id] == datetime(2019, 9, 9), "the name wins over the sidecar"
    assert found[upload.id] is None, "an upload time is no capture time"
    assert found[lost.id] is None


def test_a_doubtful_batch_with_one_consistent_gap_suggests_that_offset() -> None:
    clock = timedelta(hours=-1, minutes=-30)
    shots = run(1, 2, 3, 4, 5, 6)
    for n in (2, 3):
        good = shots[n - 1].taken
        assert good is not None
        shots[n - 1] = item(
            f"IMG_{n:04d}.JPG", taken=good - clock - 3 * DAY, sidecar_taken=good.replace(tzinfo=UTC)
        )

    found = audit(shots, NOW)
    (batch,) = batches(shots, found, suggestions(shots, found))

    assert batch.offset == 3 * DAY + clock


def test_a_batch_without_a_consistent_gap_suggests_no_offset() -> None:
    a = item("IMG_0001.JPG", taken=None)

    (batch,) = batches([a], audit([a], NOW), {a.id: datetime(2019, 1, 1)})

    assert batch.offset is None


def test_suggestions_are_whole_seconds() -> None:
    first = item("IMG_0001.JPG", taken=datetime(2019, 7, 1, 12, 0, 0))
    gap = [item(f"IMG_{n:04d}.JPG", taken=None) for n in (2, 3)]
    last = item("IMG_0004.JPG", taken=datetime(2019, 7, 1, 12, 0, 1))
    shots = [first, *gap, last]

    found = suggestions(shots, audit(shots, NOW))

    assert [found[s.id] for s in gap] == [datetime(2019, 7, 1, 12, 0, 0)] * 2
