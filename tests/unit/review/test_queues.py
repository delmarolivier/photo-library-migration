"""The Dates queue leaves out what the other queues already drop."""

from pathlib import Path

import pytest

from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.review.queues import date_cards, dropped

ROOT = "Takeout/Google Photos/Photos from 2011"


@pytest.fixture
def cat(tmp_path: Path) -> Catalogue:
    catalogue = Catalogue.open(tmp_path / "catalogue.sqlite")
    yield catalogue  # type: ignore[misc]
    catalogue.close()


def google(cat: Catalogue, name: str, **changes: object) -> int:
    return cat.upsert_item(
        CatalogueItem(source="google", source_key=f"{ROOT}/{name}", kind="photo").replace(**changes)
    )


def name(cat: Catalogue, item_id: int) -> str:
    item = cat.item(item_id)
    return f"{item.source}:{item.source_key}"


def group(
    cat: Catalogue, kind: str, key: str, members: list[tuple[int, str]], **details: object
) -> int:
    group_id = cat.upsert_group(kind, key, details)  # type: ignore[arg-type]
    cat.set_group_members(group_id, [(i, role, rank) for rank, (i, role) in enumerate(members)])
    return group_id


def batch(cat: Catalogue, key: str, *items: int) -> int:
    return group(cat, "date-batch", key, [(i, "doubtful") for i in items], problem="doubtful",
                 camera=None, folder="f", prefix="IMG_", current={}, suggestions={})  # fmt: skip


def test_clutter_is_dropped_until_the_review_keeps_it(cat: Catalogue) -> None:
    junk = google(cat, "screenshot.png")
    cat.set_clutter(junk, "screenshot", 0.9)

    assert dropped(cat) == {junk}
    cat.set_decision("item", junk, "clutter", {"reject": False})
    assert dropped(cat) == set()


def test_copies_are_dropped_unless_the_review_says_they_are_not_duplicates(
    cat: Catalogue,
) -> None:
    keeper, copy = google(cat, "IMG_1.JPG"), google(cat, "IMG_1(1).JPG")
    dup = group(cat, "duplicate", "d", [(keeper, "keeper"), (copy, "duplicate")],
                keeper=name(cat, keeper))  # fmt: skip

    assert dropped(cat) == {copy}
    cat.set_decision("group", dup, "duplicate", {"keeper": name(cat, copy)})
    assert dropped(cat) == {keeper}
    cat.set_decision("group", dup, "duplicate", {"reject": True})
    assert dropped(cat) == set()


@pytest.mark.parametrize("kind", ["best-shot", "highlights"])
def test_shots_left_out_of_the_picks_are_dropped(cat: Catalogue, kind: str) -> None:
    a, b = google(cat, "IMG_2.JPG"), google(cat, "IMG_3.JPG")
    picks = group(cat, kind, "p", [(a, "keep"), (b, "to-delete")], keep=[name(cat, a)])

    assert dropped(cat) == {b}
    cat.set_decision("group", picks, kind, {"keep": [name(cat, b)]})
    assert dropped(cat) == {a}


def test_a_pair_that_keeps_both_drops_nothing(cat: Catalogue) -> None:
    a, b = google(cat, "IMG_4.JPG"), google(cat, "IMG_5.JPG")
    group(cat, "best-shot", "p", [(a, "keep"), (b, "keep")], keep=[name(cat, a), name(cat, b)])

    assert dropped(cat) == set()


def test_motion_clips_and_edits_go_with_their_photo(cat: Catalogue) -> None:
    still, motion = google(cat, "IMG_6.HEIC"), google(cat, "IMG_6.MP4", kind="video")
    original, edit = google(cat, "IMG_7.JPG"), google(cat, "IMG_7-edited.JPG")
    group(cat, "live-pair", "lp", [(still, "still"), (motion, "motion")])
    group(cat, "edit-pair", "ep", [(original, "original"), (edit, "edited")], keep=[])
    cat.set_clutter(still, "screenshot", 0.9)
    cat.set_clutter(original, "meme", 0.9)

    assert dropped(cat) == {still, motion, original, edit}


def test_the_dates_queue_shows_only_what_is_left_and_skips_empty_batches(cat: Catalogue) -> None:
    junk, photo, more = google(cat, "a.png"), google(cat, "IMG_8.JPG"), google(cat, "b.png")
    for item in (junk, more):
        cat.set_clutter(item, "screenshot", 0.9)
    mixed = batch(cat, "mixed", junk, photo)
    batch(cat, "all-junk", more)

    cards = date_cards(cat)

    assert [c.subject_id for c in cards] == [mixed]
    assert [m.item_id for m in cards[0].members] == [photo]
