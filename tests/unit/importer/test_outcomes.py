"""What becomes of each item, from the proposals and the review's decisions."""

from pathlib import Path

import pytest

from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.importer.outcomes import Outcome, outcomes, to_delete

ROOT = "Takeout/Google Photos/Photos from 2021"


@pytest.fixture
def cat(tmp_path: Path) -> Catalogue:
    catalogue = Catalogue.open(tmp_path / "catalogue.sqlite")
    yield catalogue  # type: ignore[misc]
    catalogue.close()


def google(cat: Catalogue, name: str, **changes: object) -> int:
    return cat.upsert_item(
        CatalogueItem(source="google", source_key=f"{ROOT}/{name}", kind="photo").replace(**changes)
    )


def icloud(cat: Catalogue, uuid: str) -> int:
    return cat.upsert_item(CatalogueItem(source="icloud", source_key=uuid, kind="photo"))


def name(cat: Catalogue, item_id: int) -> str:
    item = cat.item(item_id)
    return f"{item.source}:{item.source_key}"


def group(
    cat: Catalogue, kind: str, key: str, members: list[tuple[int, str]], **details: object
) -> int:
    group_id = cat.upsert_group(kind, key, details)  # type: ignore[arg-type]
    cat.set_group_members(group_id, [(i, role, rank) for rank, (i, role) in enumerate(members)])
    return group_id


def test_a_plain_google_item_is_imported(cat: Catalogue) -> None:
    plain = google(cat, "IMG_0001.JPG")

    assert outcomes(cat)[plain] == Outcome(plain, "import", "")


def test_icloud_items_have_no_import_outcome(cat: Catalogue) -> None:
    assert outcomes(cat) == {}
    icloud(cat, "U1")
    assert outcomes(cat) == {}


def test_a_duplicate_of_an_icloud_item_is_not_imported_once_confirmed(cat: Catalogue) -> None:
    copy, original = google(cat, "IMG_0002.JPG"), icloud(cat, "U2")
    dup = group(cat, "duplicate", "dup-1", [(original, "keeper"), (copy, "duplicate")],
                keeper=name(cat, original), method="exact")  # fmt: skip

    assert outcomes(cat)[copy] == Outcome(copy, "pending", "duplicates")
    cat.set_decision("group", dup, "duplicate", {"keeper": name(cat, original)})
    assert outcomes(cat)[copy] == Outcome(copy, "duplicate-of-existing", "duplicates", original)


def test_the_review_can_choose_another_keeper_or_say_not_duplicates(cat: Catalogue) -> None:
    a, b = google(cat, "IMG_0003.JPG"), google(cat, "IMG_0003_2.jpg")
    dup = group(cat, "duplicate", "dup-2", [(a, "keeper"), (b, "duplicate")],
                keeper=name(cat, a), method="near")  # fmt: skip

    cat.set_decision("group", dup, "duplicate", {"keeper": name(cat, b)})
    found = outcomes(cat)
    assert (found[a].status, found[a].partner, found[b].status) == (
        "duplicate-of-existing",
        b,
        "import",
    )

    cat.set_decision("group", dup, "duplicate", {"reject": True})
    assert {outcomes(cat)[i].status for i in (a, b)} == {"import"}


def test_best_shot_losers_are_rejected_once_the_review_picks(cat: Catalogue) -> None:
    a, b, c = (google(cat, f"IMG_01{n}.JPG") for n in range(3))
    shot = group(cat, "best-shot", "best-1", [(a, "keep"), (b, "keep"), (c, "to-delete")],
                 keep=[name(cat, a), name(cat, b)])  # fmt: skip

    assert outcomes(cat)[c].status == "pending"
    assert outcomes(cat)[a].status == "pending", "nothing in an unanswered group goes yet"
    cat.set_decision("group", shot, "best-shot", {"keep": [name(cat, a), name(cat, c)]})
    found = outcomes(cat)
    assert (found[a].status, found[b], found[c].status) == (
        "import", Outcome(b, "rejected-by-user", "best-shot"), "import",
    )  # fmt: skip


def test_edit_pairs_follow_their_policy_until_the_review_changes_it(cat: Catalogue) -> None:
    original = google(cat, "IMG_0200.JPG")
    edit = google(cat, "IMG_0200-edited.JPG", flags=frozenset({"edited"}))
    keys = [f"{ROOT}/IMG_0200.JPG", f"{ROOT}/IMG_0200-edited.JPG"]
    pair = group(cat, "edit-pair", keys[1], [(original, "original"), (edit, "edited")],
                 policy="pair", keep=keys)  # fmt: skip

    assert {outcomes(cat)[i].status for i in (original, edit)} == {"import"}
    cat.set_decision("group", pair, "edit-pair", {"keep": keys[:1]})
    assert outcomes(cat)[edit] == Outcome(edit, "rejected-by-user", "edited version")
    assert outcomes(cat)[original].status == "import"


def test_a_live_photos_motion_clip_follows_its_still(cat: Catalogue) -> None:
    still = google(cat, "IMG_0300.HEIC")
    motion = google(cat, "IMG_0300.MOV", flags=frozenset({"live-photo-motion"}))
    group(cat, "live-pair", f"{ROOT}/IMG_0300.HEIC", [(still, "still"), (motion, "motion")])

    assert outcomes(cat)[motion] == Outcome(motion, "follows", "live photo", still)


@pytest.mark.parametrize("kind", ["clutter", "quality", "unmatched"])
def test_items_the_review_rejects_are_not_imported(cat: Catalogue, kind: str) -> None:
    item = google(cat, "IMG_0400.JPG")

    cat.set_decision("item", item, kind, {"reject": True})

    assert outcomes(cat)[item] == Outcome(item, "rejected-by-user", kind)
    cat.set_decision("item", item, kind, {"reject": False})
    assert outcomes(cat)[item].status == "import"


def test_google_creations_wait_for_the_review(cat: Catalogue) -> None:
    creation = google(cat, "IMG_0500-MOTION.gif", flags=frozenset({"google-creation"}))
    cat.set_clutter(creation, "google-creation", 1.0, {"creation-flag": 1.0})

    assert outcomes(cat)[creation] == Outcome(creation, "pending", "google creation")
    cat.set_decision("item", creation, "clutter", {"reject": False})
    assert outcomes(cat)[creation].status == "import"


def test_a_rejection_wins_over_a_pending_question(cat: Catalogue) -> None:
    a, b = google(cat, "IMG_0600.JPG"), google(cat, "IMG_0601.JPG")
    group(cat, "best-shot", "best-2", [(a, "keep"), (b, "keep")], keep=[])
    group(cat, "best-shot", "best-3", [(a, "keep"), (b, "to-delete")], keep=[name(cat, a)])
    cat.set_decision("item", b, "quality", {"reject": True})

    assert outcomes(cat)[b] == Outcome(b, "rejected-by-user", "quality")


def test_icloud_items_the_review_rejects_go_to_to_delete(cat: Catalogue) -> None:
    keeper, worse = google(cat, "IMG_0700.JPG"), icloud(cat, "U7")
    dup = group(cat, "duplicate", "dup-7", [(keeper, "keeper"), (worse, "duplicate")],
                keeper=name(cat, keeper), method="near")  # fmt: skip
    a, b = icloud(cat, "U8"), icloud(cat, "U9")
    shot = group(cat, "best-shot", "best-8", [(a, "keep"), (b, "to-delete")], keep=[name(cat, a)])
    junk = icloud(cat, "U10")

    assert to_delete(cat) == {}, "nothing goes before the review answers"
    cat.set_decision("group", dup, "duplicate", {"keeper": name(cat, keeper)})
    cat.set_decision("group", shot, "best-shot", {"keep": [name(cat, a)]})
    cat.set_decision("item", junk, "clutter", {"reject": True})
    cat.set_decision("item", a, "quality", {"reject": False})

    assert to_delete(cat) == {worse: "duplicate", b: "best-shot", junk: "clutter"}


def test_an_icloud_keeper_is_never_staged(cat: Catalogue) -> None:
    keeper, copy = icloud(cat, "U11"), google(cat, "IMG_0800.JPG")
    dup = group(cat, "duplicate", "dup-8", [(keeper, "keeper"), (copy, "duplicate")],
                keeper=name(cat, keeper), method="exact")  # fmt: skip
    cat.set_decision("group", dup, "duplicate", {"keeper": name(cat, keeper)})

    assert to_delete(cat) == {}


def test_an_edit_of_a_google_creation_follows_its_originals_answer(cat: Catalogue) -> None:
    creation = google(cat, "IMG_0900-SMILE.jpg", flags=frozenset({"google-creation"}))
    edit = google(cat, "IMG_0900-SMILE-edited.jpg", flags=frozenset({"google-creation", "edited"}))
    keys = [f"{ROOT}/IMG_0900-SMILE.jpg", f"{ROOT}/IMG_0900-SMILE-edited.jpg"]
    group(cat, "edit-pair", keys[1], [(creation, "original"), (edit, "edited")],
          policy="pair", keep=keys)  # fmt: skip

    assert outcomes(cat)[edit].status == "pending"
    cat.set_decision("item", creation, "clutter", {"reject": True})
    assert outcomes(cat)[edit] == Outcome(edit, "rejected-by-user", "clutter")
    cat.set_decision("item", creation, "clutter", {"reject": False})
    assert outcomes(cat)[edit].status == "import"


def test_a_best_shot_pair_keeps_both_without_being_asked(cat: Catalogue) -> None:
    a, b = google(cat, "IMG_1001.JPG"), google(cat, "IMG_1002.JPG")
    group(cat, "best-shot", "best-pair", [(a, "keep"), (b, "keep")],
          keep=[name(cat, a), name(cat, b)])  # fmt: skip

    assert {outcomes(cat)[i].status for i in (a, b)} == {"import"}, "the gallery never asks"
