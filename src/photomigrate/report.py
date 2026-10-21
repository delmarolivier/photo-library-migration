"""``photomigrate report``: reconciliation, every Google item accounted for (CLA-91).

Each Google item (an album copy counts with its item) gets exactly one status:

* ``imported``, with its Photos uuid;
* ``duplicate-of-existing``, with the uuid of the copy that stays: the iCloud item, or the
  Google keeper once that is imported;
* ``rejected-by-user``, with the reason;
* ``unaccounted``: not imported yet, waiting for a review answer, a duplicate whose keeper is
  not imported yet, or a Takeout file ingest logged but never catalogued.

The report also gives counts and bytes per source and status, the rejection reasons, the
SHA-256 trail (Takeout file, working copy, imported asset), the date and location fixes, and
how many iCloud items are staged in "To Delete". ``report`` exits non-zero while anything is
unaccounted, which is the migration's finish line.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal, cast

from jinja2 import Environment, FileSystemLoader, select_autoescape

from photomigrate.analyse.locations import approved_location
from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.importer.outcomes import Outcome, outcomes

Status = Literal["imported", "duplicate-of-existing", "rejected-by-user", "unaccounted"]
_TEMPLATES = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class Line:
    path: str
    kind: str
    size: int | None
    status: Status
    detail: str
    copies: int = 0
    sha256: str | None = None
    working_sha256: str | None = None
    photos_uuid: str | None = None


@dataclass(frozen=True)
class Report:
    lines: list[Line]
    icloud: list[CatalogueItem]
    staged_ids: frozenset[int]
    date_fixes: int
    location_fixes: int

    @property
    def staged(self) -> int:
        return len(self.staged_ids)

    @property
    def imported_assets(self) -> int:
        """Distinct Photos assets imported: what "From Google" shows (a Live Photo's still and
        clip are two items but one asset)."""
        return len({line.photos_uuid for line in self.lines if line.status == "imported"})

    @property
    def unaccounted(self) -> list[Line]:
        return [line for line in self.lines if line.status == "unaccounted"]

    def totals(self) -> dict[str, dict[str, tuple[int, int]]]:
        """source -> {status: (count, bytes)}, with "all"."""
        google: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for line in self.lines:
            for status in (line.status, "all"):
                google[status][0] += 1
                google[status][1] += line.size or 0
        icloud = {
            "all": (len(self.icloud), sum(i.size or 0 for i in self.icloud)),
            "staged for To Delete": (
                self.staged,
                sum(i.size or 0 for i in self.icloud if i.id in self.staged_ids),
            ),
        }
        return {"google": {k: (v[0], v[1]) for k, v in google.items()}, "icloud": icloud}

    def reasons(self) -> dict[str, int]:
        rejected = (line.detail for line in self.lines if line.status == "rejected-by-user")
        return dict(Counter(rejected))


def _line(
    item: CatalogueItem,
    outcome: Outcome,
    imported: dict[int, tuple[str, str, str]],
    found: dict[int, Outcome],
    catalogue: Catalogue,
    copies: int,
) -> Line:
    def line(status: Status, detail: str, working: str | None = None) -> Line:
        uuid = detail if status == "imported" else None
        return Line(item.source_key, item.kind, item.size, status, detail, copies, item.sha256,
                    working, uuid)  # fmt: skip

    record = imported.get(cast(int, item.id))
    if record is not None:
        return line("imported", record[0], record[1])
    if outcome.status == "duplicate-of-existing":
        partner = catalogue.item(cast(int, outcome.partner))
        if partner.source == "icloud":
            return line("duplicate-of-existing", partner.source_key)
        if partner.id in imported:
            return line("duplicate-of-existing", imported[partner.id][0])
        return line("unaccounted", "its keeper is not imported yet")
    if outcome.status == "rejected-by-user":
        return line("rejected-by-user", outcome.reason)
    if outcome.status == "pending":
        return line("unaccounted", f"waiting for review: {outcome.reason}")
    if outcome.status == "follows":
        still = found.get(cast(int, outcome.partner))
        if still is not None and still.status == "rejected-by-user":
            return line("rejected-by-user", still.reason)
    return line("unaccounted", "not imported yet")


def reconcile(catalogue: Catalogue) -> Report:
    found = outcomes(catalogue)
    imported = catalogue.imports()
    copies = Counter(item_id for _, item_id in catalogue.copies("google"))
    lines = [
        _line(
            item, found[cast(int, item.id)], imported, found, catalogue, copies[cast(int, item.id)]
        )
        for item in catalogue.items("google")
    ]
    for target, keys in sorted(catalogue.applied_actions("ingest").items()):
        if any(key.startswith("ingest:") for key in keys) and (
            catalogue.item_for_key("google", target) is None
        ):
            lines.append(Line(target, "?", None, "unaccounted", "not catalogued"))
    icloud = catalogue.items("icloud")
    every = catalogue.items()
    return Report(
        lines=sorted(lines, key=lambda line: line.path),
        icloud=icloud,
        staged_ids=frozenset(catalogue.staged()),
        date_fixes=sum(1 for i in every if "user" in catalogue.date_candidates(cast(int, i.id))),
        location_fixes=sum(
            1 for i in catalogue.inferred_locations() if approved_location(catalogue, i)
        ),
    )


def write_csv(report: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[f.name for f in fields(Line)])
        writer.writeheader()
        for line in report.lines:
            writer.writerow(asdict(line))


def write_html(report: Report, path: Path) -> None:
    environment = Environment(
        loader=FileSystemLoader(_TEMPLATES), autoescape=select_autoescape(["html"])
    )
    page = environment.get_template("report.html").render(report=report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
