"""The review queues: what the gallery shows, read from the catalogue, one card per question.

A card is one question with one answer, stored as a decision: about a group (duplicates,
best shot, edit pairs, date batches) or an item (clutter, quality, locations, unmatched).
Each card has a proposal (the answer the analysis suggests, accepted with one key) and
the other answers it allows.

Decisions:

* ``duplicate`` (group): ``{"keeper": "source:key"}`` (the copies are duplicates, kept as this
  one) or ``{"reject": true}`` (not duplicates);
* ``best-shot`` (group): ``{"keep": ["source:key", ...]}``, the rest go to "To Delete";
* ``highlights`` (group): the same, for one event of a year (it may keep none);
* ``edit-pair`` (group): ``{"keep": ["Takeout path", ...]}``;
* ``date`` (group): the answers in ``analyse.date_fixes``;
* ``clutter``, ``quality``, ``unmatched`` (item): ``{"reject": true}`` (do not import, or move to
  "To Delete" in iCloud) or ``{"reject": false}``;
* ``location`` (item): ``{"approve": true}`` or ``{"approve": false}``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from photomigrate.catalogue import Catalogue, CatalogueItem, Subject

Value = dict[str, Any]


@dataclass(frozen=True)
class Member:
    item_id: int
    name: str
    """``source:key``."""
    label: str
    role: str | None = None
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class Option:
    label: str
    value: Value
    key: str | None = None
    """The keyboard shortcut."""


@dataclass(frozen=True)
class Card:
    subject: Subject
    subject_id: int
    kind: str
    title: str
    members: tuple[Member, ...]
    proposal: Option
    options: tuple[Option, ...] = ()
    decision: Value | None = None
    form: Literal["buttons", "date", "best-shot"] = "buttons"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Queue:
    name: str
    title: str
    kind: str
    subject: Subject
    cards: Callable[[Catalogue], list[Card]]
    help: str = ""


def _name(item: CatalogueItem) -> str:
    return f"{item.source}:{item.source_key}"


def _label(item: CatalogueItem) -> str:
    if item.source == "google":
        return item.source_key.rsplit("/", 1)[-1]
    return item.title or item.source_key


def _member(item: CatalogueItem, role: str | None = None, *lines: str) -> Member:
    return Member(int(item.id or 0), _name(item), _label(item), role, tuple(lines))


def _members(catalogue: Catalogue, group_id: int) -> list[tuple[CatalogueItem, str | None]]:
    return [(catalogue.item(i), role) for i, role, _ in catalogue.group_members(group_id)]


REJECT = Option("Reject", {"reject": True}, "r")
KEEP_ITEM = Option("Keep", {"reject": False}, "k")


def duplicate_cards(catalogue: Catalogue) -> list[Card]:
    cards = []
    for group in catalogue.groups("duplicate"):
        members = _members(catalogue, group.id)
        keeper = str(group.details["keeper"])
        options = [
            Option(f"Keep {_label(item)}", {"keeper": _name(item)}, str(n))
            for n, (item, role) in enumerate(members, start=1)
            if role != "motion"
        ]
        cards.append(
            Card(
                subject="group",
                subject_id=group.id,
                kind="duplicate",
                title=f"{len(members)} copies ({group.details['method']})",
                members=tuple(_member(item, role) for item, role in members),
                proposal=Option("Accept the keeper", {"keeper": keeper}, "a"),
                options=(*options, Option("Not duplicates", {"reject": True}, "r")),
                decision=catalogue.decision("group", group.id, "duplicate"),
            )
        )
    return cards


def best_shot_cards(catalogue: Catalogue) -> list[Card]:
    cards = []
    for group in catalogue.groups("best-shot"):
        members = _members(catalogue, group.id)
        if not any(role == "to-delete" for _, role in members):
            continue  # a pair keeps both: nothing to ask
        scores = group.details.get("scores", {})
        shown = tuple(
            _member(item, role, f"score {scores[_name(item)]:.2f}" if _name(item) in scores else "")
            for item, role in members
        )
        cards.append(
            Card(
                subject="group",
                subject_id=group.id,
                kind="best-shot",
                title=f"{len(scores)} similar shots",
                members=shown,
                proposal=Option("Keep the top two", {"keep": list(group.details["keep"])}, "a"),
                decision=catalogue.decision("group", group.id, "best-shot"),
                form="best-shot",
            )
        )
    return cards


def highlights_cards(catalogue: Catalogue) -> list[Card]:
    cards = []
    for group in catalogue.groups("highlights"):
        details = group.details
        members = _members(catalogue, group.id)
        scores, protected = details.get("scores", {}), details.get("protected", {})
        shown = tuple(
            _member(
                item,
                role,
                *([f"score {scores[_name(item)]:.2f}"] if _name(item) in scores else []),
                *([f"always kept: {protected[_name(item)]}"] if _name(item) in protected else []),
            )
            for item, role in members
        )
        kept = list(details["keep"])
        cards.append(
            Card(
                subject="group",
                subject_id=group.id,
                kind="highlights",
                title=(
                    f"{details['year']} {details.get('kind', 'photo')}s: {len(scores)}, "
                    f"{len(kept)} kept ({str(details['start'])[:10]} to {str(details['end'])[:10]})"
                ),
                members=shown,
                proposal=Option("Keep the picks", {"keep": kept}, "a"),
                decision=catalogue.decision("group", group.id, "highlights"),
                form="best-shot",
            )
        )
    return cards


def edit_pair_cards(catalogue: Catalogue) -> list[Card]:
    cards = []
    for group in catalogue.groups("edit-pair"):
        (original, _), (edited, _) = _members(catalogue, group.id)
        both = [original.source_key, edited.source_key]
        cards.append(
            Card(
                subject="group",
                subject_id=group.id,
                kind="edit-pair",
                title=f"Edited copy ({group.details['policy']})",
                members=(_member(original, "original"), _member(edited, "edited")),
                proposal=Option("Accept", {"keep": list(group.details["keep"])}, "a"),
                options=(
                    Option("Keep both", {"keep": both}, "b"),
                    Option("Original only", {"keep": both[:1]}, "o"),
                    Option("Edit only", {"keep": both[1:]}, "e"),
                ),
                decision=catalogue.decision("group", group.id, "edit-pair"),
            )
        )
    return cards


def date_cards(catalogue: Catalogue) -> list[Card]:
    cards = []
    for group in catalogue.groups("date-batch"):
        details = group.details
        current, suggested = details["current"], details["suggestions"]
        members = tuple(
            _member(
                item,
                role,
                f"now {current.get(_name(item)) or 'no date'}",
                f"suggested {suggested.get(_name(item)) or '-'}",
            )
            for item, role in _members(catalogue, group.id)
        )
        camera = details.get("camera") or "unknown camera"
        cards.append(
            Card(
                subject="group",
                subject_id=group.id,
                kind="date",
                title=f"{details['problem']}: {camera}, {details['folder']}, {details['prefix']}…",
                members=members,
                proposal=Option("Accept the suggestions", {"answer": "accept"}, "a"),
                options=(Option("Dates are right", {"answer": "keep"}, "k"),),
                decision=catalogue.decision("group", group.id, "date"),
                form="date",
                extra={"offset_hours": _hours(details.get("offset_seconds"))},
            )
        )
    return cards


def _hours(seconds: int | None) -> float | None:
    return None if seconds is None else round(seconds / 3600, 4)


def clutter_cards(catalogue: Catalogue) -> list[Card]:
    cards = []
    for item_id, (category, score, evidence) in catalogue.clutter().items():
        item = catalogue.item(item_id)
        clues = ", ".join(sorted(evidence))
        cards.append(
            Card(
                subject="item",
                subject_id=item_id,
                kind="clutter",
                title=f"{category} ({score:.2f})",
                members=(_member(item, category, clues),),
                proposal=Option("Reject", {"reject": True}, "a"),
                options=(KEEP_ITEM,),
                decision=catalogue.decision("item", item_id, "clutter"),
                extra={"category": category},
            )
        )
    return sorted(cards, key=lambda c: (c.extra["category"], c.subject_id))


def quality_cards(catalogue: Catalogue) -> list[Card]:
    cards = []
    for item_id, flags in catalogue.quality_flags().items():
        item = catalogue.item(item_id)
        shown = ", ".join(f"{flag} {score:.2f}" for flag, score in sorted(flags.items()))
        cards.append(
            Card(
                subject="item",
                subject_id=item_id,
                kind="quality",
                title=shown,
                members=(_member(item),),
                proposal=Option("Keep", {"reject": False}, "a"),
                options=(REJECT,),
                decision=catalogue.decision("item", item_id, "quality"),
            )
        )
    return cards


def tile(latitude: float, longitude: float, zoom: int = 15) -> dict[str, Any]:
    """The OpenStreetMap tile holding a place (served by the gallery, ``review.tiles``), and
    where in it the place is (percent)."""
    scale = 2**zoom
    x = (longitude + 180) / 360 * scale
    rad = math.radians(latitude)
    y = (1 - math.asinh(math.tan(rad)) / math.pi) / 2 * scale
    return {
        "url": f"/tile/{zoom}/{int(x)}/{int(y)}.png",
        "left": round((x - int(x)) * 100, 2),
        "top": round((y - int(y)) * 100, 2),
        "link": f"https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}#map=17/{latitude}/{longitude}",
    }


def location_cards(catalogue: Catalogue) -> list[Card]:
    cards = []
    for item_id, ((latitude, longitude), method, _) in catalogue.inferred_locations().items():
        item = catalogue.item(item_id)
        cards.append(
            Card(
                subject="item",
                subject_id=item_id,
                kind="location",
                title=f"{method}: {latitude:.5f}, {longitude:.5f}",
                members=(_member(item),),
                proposal=Option("Approve", {"approve": True}, "a"),
                options=(Option("Reject", {"approve": False}, "r"),),
                decision=catalogue.decision("item", item_id, "location"),
                extra={"map": tile(latitude, longitude)},
            )
        )
    return cards


UNMATCHED_FLAGS = (
    "no-sidecar",
    "bad-sidecar",
    "unpaired-motion",
    "unpaired-edit",
    "original-missing",
)


def unmatched_cards(catalogue: Catalogue) -> list[Card]:
    cards = []
    for item in catalogue.items():
        found = sorted(set(UNMATCHED_FLAGS) & item.flags)
        if not found:
            continue
        item_id = int(item.id or 0)
        cards.append(
            Card(
                subject="item",
                subject_id=item_id,
                kind="unmatched",
                title=", ".join(found),
                members=(_member(item),),
                proposal=Option("Import anyway", {"reject": False}, "a"),
                options=(REJECT,),
                decision=catalogue.decision("item", item_id, "unmatched"),
            )
        )
    return cards


QUEUES: dict[str, Queue] = {
    queue.name: queue
    for queue in (
        Queue("duplicates", "Duplicates", "duplicate", "group", duplicate_cards,
              "The proposed keeper is first; pick another with 1 to 9."),
        Queue("best-shot", "Best shot", "best-shot", "group", best_shot_cards,
              "The top two are ticked; change the ticks, then save."),
        Queue("highlights", "Highlights", "highlights", "group", highlights_cards,
              "Each year's best photos, one event at a time; change the ticks, then save."),
        Queue("edit-pairs", "Edited versions", "edit-pair", "group", edit_pair_cards,
              "Original and Google edit side by side."),
        Queue("dates", "Dates", "date", "group", date_cards,
              "One answer per batch: the suggestions, a date, a year, a shift, or remove it."),
        Queue("clutter", "Clutter", "clutter", "item", clutter_cards,
              "Screenshots, documents, memes, forwards and Google creations."),
        Queue("quality", "Quality flags", "quality", "item", quality_cards,
              "Blurry, badly exposed, accidental or tiny."),
        Queue("locations", "Locations", "location", "item", location_cards,
              "Inferred places; nothing is written until approved."),
        Queue("unmatched", "Unmatched", "unmatched", "item", unmatched_cards,
              "Items without a sidecar, or a partner that is missing."),
    )
}  # fmt: skip
KINDS = {queue.kind: queue for queue in QUEUES.values()}


def as_json(value: Value) -> str:
    return json.dumps(value, sort_keys=True)


def progress(cards: Sequence[Card]) -> tuple[int, int]:
    return sum(1 for card in cards if card.decision is not None), len(cards)
