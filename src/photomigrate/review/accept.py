"""``photomigrate accept``: record every unanswered proposal as the review's answer.

For a library too big to click through: what ``analyse`` proposes becomes the answer to each
question the gallery would ask, except dates and places, which stay with the review. By
default an original and its Google edit keep only the edit, and shots flagged for quality
(blurry, badly exposed, tiny) are dropped; ``edit_only`` and ``drop_flagged`` turn that off.
Questions already answered are left as they are, and the gallery can still change any answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

from photomigrate.catalogue import Catalogue, Subject
from photomigrate.review.queues import QUEUES, Card
from photomigrate.runner import Action, JSONValue

LEFT_TO_THE_REVIEW = ("date", "location")


class AcceptStage:
    name = "accept"

    def __init__(self, *, edit_only: bool = True, drop_flagged: bool = True) -> None:
        self.edit_only = edit_only
        self.drop_flagged = drop_flagged

    def _answer(self, card: Card) -> JSONValue:
        chosen = {"edit-pair": "e" if self.edit_only else None,
                  "quality": "r" if self.drop_flagged else None}.get(card.kind)  # fmt: skip
        option = next((o for o in card.options if o.key == chosen), card.proposal)
        return cast(JSONValue, option.value)

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        for queue in QUEUES.values():
            if queue.kind in LEFT_TO_THE_REVIEW:
                continue
            for card in queue.cards(catalogue):
                if card.decision is None:
                    yield Action(
                        "accept",
                        f"{card.kind}:{card.subject}:{card.subject_id}",
                        {"subject": card.subject, "subject_id": card.subject_id,
                         "kind": card.kind, "answer": self._answer(card)},
                    )  # fmt: skip

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        details = action.details
        subject, subject_id = cast(Subject, details["subject"]), cast(int, details["subject_id"])
        kind = str(details["kind"])
        if catalogue.decision(subject, subject_id, kind) is None:  # the review may have answered
            catalogue.set_decision(
                subject, subject_id, kind, cast(dict[str, Any], details["answer"])
            )
