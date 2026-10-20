"""``photomigrate ingest``: catalogue every Google Photos item straight from the Takeout.

Plan (read-only): list the zip parts, match media to sidecars folder by folder, and parse
the sidecars. That gives one ``ingest`` action per media file, plus ``flag-…`` actions for
orphan sidecars and unknown files, so nothing is dropped silently.

Apply, one action at a time: stream the item to a scratch file, hash it, read its embedded
metadata, decide what to write later (``policy.decide``), and record the item, its flags,
its metadata plan and every date candidate. The zips are only ever read. Working copies
are written at import time for approved items (M4-01).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, tzinfo
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self, cast

from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.ingest.exif import ExifTool, can_write
from photomigrate.ingest.names import Kind, media_kind, parse_media_name
from photomigrate.ingest.policy import date_candidates, decide
from photomigrate.ingest.sidecars import (
    Match,
    SidecarData,
    SidecarError,
    match_folder,
    parse_sidecar,
)
from photomigrate.ingest.takeout import Entry, Takeout, open_takeout
from photomigrate.runner import Action, JSONValue


class IngestStage:
    name = "ingest"

    def __init__(self, takeout_dir: Path, work_dir: Path, *, zone: tzinfo) -> None:
        self.takeout_dir = takeout_dir
        self.work_dir = work_dir
        self.zone = zone
        self._takeout: Takeout | None = None
        self._exiftool: ExifTool | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._exiftool is not None:
            self._exiftool.close()
        if self._takeout is not None:
            self._takeout.close()

    @property
    def takeout(self) -> Takeout:
        if self._takeout is None:
            self._takeout = open_takeout(self.takeout_dir)
        return self._takeout

    @property
    def exiftool(self) -> ExifTool:
        if self._exiftool is None:
            self._exiftool = ExifTool()
        return self._exiftool

    # -- plan -------------------------------------------------------------------------------

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        folders: dict[str, list[Entry]] = defaultdict(list)
        for entry in self.takeout.entries:
            folders[str(PurePosixPath(entry.path).parent)].append(entry)
        for folder in sorted(folders):
            yield from self._plan_folder(folder, folders[folder])

    def _plan_folder(self, folder: str, entries: list[Entry]) -> Iterator[Action]:
        by_name = {PurePosixPath(e.path).name: e for e in entries}
        media = [name for name in by_name if media_kind(name)]
        jsons = [name for name in by_name if name.lower().endswith(".json")]
        result = match_folder(media, jsons)
        for name in sorted(media):
            yield self._ingest_action(folder, by_name[name], result.matches[name], by_name)
        for name in result.orphans:
            yield Action("flag-orphan-sidecar", by_name[name].path, self._where(by_name[name]))
        for name, entry in sorted(by_name.items()):
            if not media_kind(name) and not name.lower().endswith(".json"):
                yield Action("flag-unknown-file", entry.path, self._where(entry))

    def _where(self, entry: Entry) -> dict[str, JSONValue]:
        return {"locator": self.takeout.locator(entry), "size": entry.size}

    def _ingest_action(
        self, folder: str, entry: Entry, match: Match, by_name: dict[str, Entry]
    ) -> Action:
        sidecar_path = None
        sidecar_data: dict[str, object] | None = None
        flags = [f"match-{match.method}"]
        if match.sidecar is not None:
            sidecar_entry = by_name[match.sidecar]
            try:
                sidecar_data = parse_sidecar(self.takeout.read(sidecar_entry.path)).to_dict()
                sidecar_path = sidecar_entry.path
            except SidecarError:
                flags.append("bad-sidecar")
        details: dict[str, JSONValue] = {
            **self._where(entry),
            "kind": media_kind(entry.path),
            "folder": PurePosixPath(folder).name,
            "sidecar": sidecar_path,
            "sidecar_data": cast(JSONValue, sidecar_data),
            "edited_of": match.original,
            "flags": cast(JSONValue, sorted(flags)),
            "modified": entry.modified.isoformat(),
        }
        return Action("ingest", entry.path, details)

    # -- apply ------------------------------------------------------------------------------

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        if action.kind != "ingest":
            return  # flag-… actions only need their entry in the action log
        details = action.details
        entry = self.takeout.entry(str(details["locator"]))
        scratch = self.work_dir / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        with self.takeout.local_copy(entry, scratch) as (copy, sha256):
            embedded = self.exiftool.read(copy)
        raw = details.get("sidecar_data")
        sidecar = SidecarData.from_dict(raw) if isinstance(raw, dict) else None
        name = PurePosixPath(entry.path).name
        decision = decide(
            sidecar=sidecar, embedded=embedded, zone=self.zone, writable=can_write(name)
        )
        flags = set(decision.flags) | {str(f) for f in details["flags"]}  # type: ignore[union-attr]
        if parse_media_name(name).edited is not None:
            flags.add("edited")
        if sidecar is not None and sidecar.origin == "auto":
            flags.add("google-creation")
        plan = decision.plan
        kind = cast(Kind, details["kind"])
        item = CatalogueItem(
            source="google",
            source_key=entry.path,
            kind=kind,
            original_path=str(details["locator"]),
            sidecar_path=None if details["sidecar"] is None else str(details["sidecar"]),
            sha256=sha256,
            width=embedded.width,
            height=embedded.height,
            duration=embedded.duration,
            latitude=embedded.latitude if embedded.latitude is not None else plan.latitude,
            longitude=embedded.longitude if embedded.longitude is not None else plan.longitude,
            altitude=embedded.altitude if embedded.latitude is not None else plan.altitude,
            title=(sidecar.title if sidecar and sidecar.title else name),
            description=(sidecar.description if sidecar and sidecar.description else None)
            or embedded.description,
            favorite=bool(sidecar and sidecar.favorited),
            flags=frozenset(flags),
            metadata_plan=json.dumps(plan.to_dict(), sort_keys=True, ensure_ascii=False),
        )
        item_id = catalogue.upsert_item(item)
        found = date_candidates(
            kind=kind,
            embedded=embedded,
            sidecar=sidecar,
            name=name,
            modified=datetime.fromisoformat(str(details["modified"])),
        )
        for source, value in found.items():
            catalogue.set_date_candidate(item_id, source, value)
