"""Shared plumbing for duplicate proposals: union-find, groups, and their catalogue actions.

Each dedupe stage owns the ``duplicate`` groups whose key starts with its prefix (``dup-`` for
photos, ``vdup-`` for videos). It proposes groups, and retires its own groups that no longer
hold. It never touches the other stage's groups.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from photomigrate.catalogue import Catalogue, Source
from photomigrate.runner import Action, JSONValue

Method = Literal["exact", "content-id", "near", "mixed"]


class Keyed(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def source(self) -> Source: ...
    @property
    def key(self) -> str: ...


@dataclass(frozen=True)
class DuplicateGroup:
    keeper: int
    members: tuple[int, ...]
    """Record ids, keeper first, then in keeper order."""
    method: Method


class UnionFind:
    def __init__(self, ids: Iterable[int]) -> None:
        self.parent = {i: i for i in ids}
        self.kinds: dict[int, set[str]] = {}

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, a: int, b: int, kind: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        kinds = self.kinds.pop(root_a, set()) | self.kinds.pop(root_b, set()) | {kind}
        self.parent[root_b] = root_a
        self.kinds[root_a] = kinds

    def components(self, records: Sequence[Keyed]) -> list[list[Keyed]]:
        """Connected records, two or more at a time."""
        by_root: dict[int, list[Keyed]] = {}
        for record in records:
            by_root.setdefault(self.find(record.id), []).append(record)
        return [members for members in by_root.values() if len(members) > 1]


def star_groups(
    components: Iterable[list[Keyed]],
    edge: Callable[[Keyed, Keyed], str | None],
    order: Callable[[list[Keyed]], list[Keyed]],
) -> list[DuplicateGroup]:
    """Split connected records into groups whose members all match their keeper *directly*.

    Chains (A~B, B~C, but A !~ C) would otherwise pull different pictures into one group.
    Each component is taken best-keeper first. The keeper gathers its direct matches, and the
    rest are grouped again the same way.
    """
    result = []
    for component in components:
        remaining = order(component)
        while remaining:
            keeper, rest = remaining[0], remaining[1:]
            kinds = {kind for other in rest if (kind := edge(keeper, other))}
            members = [keeper, *(other for other in rest if edge(keeper, other))]
            remaining = [other for other in rest if not edge(keeper, other)]
            if len(members) > 1:
                method = cast(Method, "mixed" if len(kinds) > 1 else next(iter(kinds)))
                result.append(DuplicateGroup(keeper.id, tuple(m.id for m in members), method))
    return sorted(result, key=lambda g: g.members)


def same_value_pairs(records: Iterable[Keyed], attribute: str) -> Iterator[tuple[int, int]]:
    """(first id, later id) for records sharing a non-empty value of ``attribute``."""
    seen: dict[str, int] = {}
    for record in records:
        value = getattr(record, attribute)
        if value:
            if value in seen:
                yield seen[value], record.id
            else:
                seen[value] = record.id


def proposal_actions(
    catalogue: Catalogue,
    groups: Iterable[DuplicateGroup],
    by_id: Mapping[int, Keyed],
    prefix: str,
    followers: Mapping[int, tuple[Source, str, str]] | None = None,
) -> Iterator[Action]:
    """``propose-duplicates`` per group, and ``retire-duplicates`` for this prefix's stale ones.

    ``followers`` adds a member right after a record, such as a Live Photo still's motion clip.
    """
    wanted = set()
    for group in groups:
        members = [by_id[i] for i in group.members]
        names = "|".join(sorted(f"{m.source}:{m.key}" for m in members))
        key = prefix + hashlib.sha256(names.encode()).hexdigest()[:16]
        wanted.add(key)
        listed: list[JSONValue] = []
        for record in members:
            listed.append(
                [record.source, record.key, "keeper" if record.id == group.keeper else "duplicate"]
            )
            if followers and record.id in followers:
                listed.append(list(followers[record.id]))
        keeper = by_id[group.keeper]
        details: dict[str, JSONValue] = {
            "keeper": f"{keeper.source}:{keeper.key}",
            "method": group.method,
            "members": listed,
        }
        yield Action("propose-duplicates", key, details)
    for stale in catalogue.groups("duplicate"):
        if stale.key.startswith(prefix) and stale.key not in wanted:
            yield Action("retire-duplicates", stale.key)


def apply_proposal(action: Action, catalogue: Catalogue) -> None:
    if action.kind == "retire-duplicates":
        catalogue.discard_group("duplicate", action.target)
        return
    details = action.details
    group = catalogue.upsert_group(
        "duplicate", action.target, {"keeper": details["keeper"], "method": details["method"]}
    )
    members = []
    for rank, (source, key, role) in enumerate(cast(list[list[str]], details["members"])):
        item_id = catalogue.item_for_key(cast(Source, source), key)
        if item_id is None:
            raise KeyError(f"{source}:{key} is no longer in the catalogue")
        members.append((item_id, role, rank))
    catalogue.set_group_members(group, members)
