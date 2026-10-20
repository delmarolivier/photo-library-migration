"""What becomes of each item: the proposals of ``analyse`` plus the review's decisions.

Every Google item gets exactly one outcome:

* ``import``: it goes into iCloud Photos;
* ``duplicate-of-existing``: the review confirmed it is a copy of ``partner``, which is in
  iCloud or imported;
* ``rejected-by-user``: the review said no (a best-shot loser, clutter, quality, unmatched,
  or the version of an edit pair the review did not keep);
* ``follows``: a Live Photo's motion clip, imported with its still (``partner``);
* ``pending``: a question that decides it is still unanswered (a duplicate or best-shot
  group, or a Google creation, which is never imported without a yes).

A confirmed duplicate wins over a rejection, a rejection over a pending question.

Existing iCloud items the review rejected (a confirmed duplicate of another item, a
best-shot loser, clutter, quality or unmatched) go to the "To Delete" album (``to_delete``):
Delmar deletes them by hand. Nothing here deletes anything.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from photomigrate.catalogue import Catalogue, CatalogueItem

Status = Literal["import", "duplicate-of-existing", "rejected-by-user", "follows", "pending"]
ITEM_REJECTIONS = ("clutter", "quality", "unmatched")


@dataclass(frozen=True)
class Outcome:
    item_id: int
    status: Status
    reason: str
    partner: int | None = None


def _name(item: CatalogueItem) -> str:
    return f"{item.source}:{item.source_key}"


@dataclass
class _Verdicts:
    duplicate_of: dict[int, int | None] = field(default_factory=dict)
    rejected: dict[int, list[str]] = field(default_factory=lambda: defaultdict(list))
    pending: dict[int, list[str]] = field(default_factory=lambda: defaultdict(list))
    follows: dict[int, int] = field(default_factory=dict)


def _verdicts(catalogue: Catalogue) -> _Verdicts:
    found = _Verdicts()
    names = {int(item.id or 0): _name(item) for item in catalogue.items()}
    for group in catalogue.groups("live-pair"):
        roles = {role: i for i, role, _ in catalogue.group_members(group.id)}
        found.follows[roles["motion"]] = roles["still"]
    for group in catalogue.groups("duplicate"):
        members = [
            (i, role) for i, role, _ in catalogue.group_members(group.id) if role != "motion"
        ]
        answer = catalogue.decision("group", group.id, "duplicate")
        if answer is None:
            for i, _ in members:
                found.pending[i].append("duplicates")
        elif not answer.get("reject"):
            keeper = next((i for i, _ in members if names[i] == answer.get("keeper")), None)
            for i, _ in members:
                if i != keeper:
                    found.duplicate_of[i] = keeper
    for group in catalogue.groups("best-shot"):
        shots = [i for i, role, _ in catalogue.group_members(group.id) if role != "motion"]
        answer = catalogue.decision("group", group.id, "best-shot")
        for i in shots:
            if answer is None:
                found.pending[i].append("best-shot")
            elif names[i] not in answer.get("keep", []):
                found.rejected[i].append("best-shot")
    for group in catalogue.groups("edit-pair"):
        (original, _, _), (edited, _, _) = catalogue.group_members(group.id)
        answer = catalogue.decision("group", group.id, "edit-pair") or {}
        keep = answer.get("keep", group.details.get("keep", []))
        for i, reason in ((original, "original version"), (edited, "edited version")):
            if catalogue.item(i).source_key not in keep:
                found.rejected[i].append(reason)
    for item_id in names:
        for kind in ITEM_REJECTIONS:
            answer = catalogue.decision("item", item_id, kind)
            if answer is not None and answer.get("reject") is True:
                found.rejected[item_id].append(kind)
    return found


def outcomes(catalogue: Catalogue) -> dict[int, Outcome]:
    """Every Google item's outcome."""
    found = _verdicts(catalogue)
    result = {}
    for item in catalogue.items("google"):
        item_id = int(item.id or 0)
        if item_id in found.follows:
            result[item_id] = Outcome(item_id, "follows", "live photo", found.follows[item_id])
        elif item_id in found.duplicate_of:
            partner = found.duplicate_of[item_id]
            result[item_id] = Outcome(item_id, "duplicate-of-existing", "duplicates", partner)
        elif found.rejected.get(item_id):
            result[item_id] = Outcome(item_id, "rejected-by-user", found.rejected[item_id][0])
        elif found.pending.get(item_id):
            result[item_id] = Outcome(item_id, "pending", found.pending[item_id][0])
        elif "google-creation" in item.flags and catalogue.decision("item", item_id, "clutter") != {
            "reject": False
        }:
            result[item_id] = Outcome(item_id, "pending", "google creation")
        else:
            result[item_id] = Outcome(item_id, "import", "")
    return result


def to_delete(catalogue: Catalogue) -> dict[int, str]:
    """The existing iCloud items the review rejected, with why, for the "To Delete" album."""
    found = _verdicts(catalogue)
    result = {}
    for item in catalogue.items("icloud"):
        item_id = int(item.id or 0)
        if item_id in found.duplicate_of:
            result[item_id] = "duplicate"
        elif found.rejected.get(item_id):
            result[item_id] = found.rejected[item_id][0]
    return result
