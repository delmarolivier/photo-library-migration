"""Edited versions: link each Takeout ``-edited`` copy to its original (CLA-82).

Ingest flags the copies (localised suffixes and cut suffixes included). This stage finds each
one's original in the same folder, using the rule the sidecar matcher uses: same stem and
``(N)`` index, exact extension first. Album copies resolve to their one item. Each link is an
``edit-pair`` group (original rank 0, edit rank 1). Its details say what to keep under the
``edited_policy``:

* ``pair`` (default): keep both, linked;
* ``original-only``: keep the original;
* ``edited-only``: keep the edit.

The importer (M4-01) reads ``keep``. Dedupe never pairs an edit with its original, because
edits are left out of photo dedupe entirely. An edit whose original is not in the export is
flagged ``unpaired-edit``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Literal, cast

from photomigrate.catalogue import Catalogue
from photomigrate.ingest.names import MediaName, parse_media_name
from photomigrate.ingest.sidecars import original_of
from photomigrate.runner import Action, JSONValue

EditedPolicy = Literal["pair", "original-only", "edited-only"]
POLICIES: tuple[EditedPolicy, ...] = ("pair", "original-only", "edited-only")


def _keep(policy: EditedPolicy, original: str, edited: str) -> list[JSONValue]:
    if policy == "original-only":
        return [original]
    if policy == "edited-only":
        return [edited]
    return [original, edited]


class EditedVersionsStage:
    name = "edited-versions"

    def __init__(self, policy: EditedPolicy = "pair") -> None:
        if policy not in POLICIES:
            raise ValueError(f"edited_policy must be one of {', '.join(POLICIES)}")
        self.policy = policy

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        folders: dict[str, dict[str, MediaName]] = defaultdict(dict)
        paths = [item.source_key for item in catalogue.items("google")]
        paths += [path for path, _ in catalogue.copies("google")]
        for path in paths:
            where = PurePosixPath(path)
            folders[str(where.parent)][where.name] = parse_media_name(where.name)
        wanted = set()
        for item in catalogue.items("google"):
            if "edited" not in item.flags:
                continue
            where = PurePosixPath(item.source_key)
            folder = folders[str(where.parent)]
            name = original_of(folder[where.name], folder)
            original_id = (
                catalogue.item_for_key("google", f"{where.parent}/{name}") if name else None
            )
            if original_id is None:
                yield Action("flag-unpaired-edit", item.source_key)
                continue
            original = catalogue.item(original_id).source_key
            wanted.add(item.source_key)
            details: dict[str, JSONValue] = {
                "original": original,
                "policy": self.policy,
                "keep": _keep(self.policy, original, item.source_key),
            }
            yield Action("pair-edit", item.source_key, details)
        for stale in catalogue.groups("edit-pair"):
            if stale.key not in wanted:
                yield Action("unpair-edit", stale.key)

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        if action.kind == "unpair-edit":
            catalogue.discard_group("edit-pair", action.target)
            return
        edited = cast(int, catalogue.item_for_key("google", action.target))
        if action.kind == "flag-unpaired-edit":
            catalogue.add_flags(edited, {"unpaired-edit"})
            return
        original = cast(int, catalogue.item_for_key("google", str(action.details["original"])))
        group = catalogue.upsert_group("edit-pair", action.target, dict(action.details))
        catalogue.set_group_members(group, [(original, "original", 0), (edited, "edited", 1)])
        catalogue.add_flags(original, {"has-edit"})
