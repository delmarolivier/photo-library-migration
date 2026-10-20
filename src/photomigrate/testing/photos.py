"""A disposable Photos library for tests: a folder that behaves like the importer's target.

Real Photos.app libraries can only be written by driving Photos.app, which acts on whatever
library it has open; tests must never do that (CLAUDE.md rule 4). This library keeps each
imported asset's files (copied in), its uuid, albums, favourite, caption, keywords, date and
place in ``library.json``, so tests can check exactly what an import did, across restarts.
"""

from __future__ import annotations

import json
import shutil
import uuid as uuids
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


class DisposableLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._state = self.root / "library.json"
        if not self._state.exists():
            self._save({"assets": {}, "albums": {}})

    def _load(self) -> dict[str, Any]:
        return json.loads(self._state.read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def _save(self, state: dict[str, Any]) -> None:
        self._state.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def asset(self, uuid: str) -> dict[str, Any]:
        return self._load()["assets"][uuid]  # type: ignore[no-any-return]

    def assets(self) -> dict[str, dict[str, Any]]:
        return self._load()["assets"]  # type: ignore[no-any-return]

    def albums(self) -> dict[str, list[str]]:
        return self._load()["albums"]  # type: ignore[no-any-return]

    def add_existing(self, uuid: str, filename: str) -> None:
        """An asset already in the library (an iCloud item), for fixes and staging."""
        state = self._load()
        state["assets"][uuid] = _blank([filename])
        self._save(state)

    def import_assets(self, assets: Sequence[Sequence[Path]], album: str) -> list[str]:
        state = self._load()
        made = []
        for files in assets:
            uuid = str(uuids.uuid4()).upper()
            folder = self.root / "originals" / uuid
            folder.mkdir(parents=True)
            for path in files:
                shutil.copy2(path, folder / Path(path).name)
            state["assets"][uuid] = _blank([Path(p).name for p in files])
            made.append(uuid)
        state["albums"].setdefault(album, []).extend(made)
        self._save(state)
        return made

    def add_to_album(self, album: str, uuids_: Sequence[str]) -> None:
        state = self._load()
        members = state["albums"].setdefault(album, [])
        members.extend(u for u in uuids_ if u not in members)
        self._save(state)

    def set_details(
        self, uuid: str, *, favorite: bool, description: str | None, keywords: Sequence[str]
    ) -> None:
        state = self._load()
        asset = state["assets"][uuid]
        asset["favorite"] = favorite
        if description is not None:
            asset["description"] = description
        asset["keywords"] = sorted(set(asset["keywords"]) | set(keywords))
        self._save(state)

    def set_date(self, uuid: str, when: datetime) -> None:
        state = self._load()
        state["assets"][uuid]["date"] = when.isoformat()
        self._save(state)

    def set_location(self, uuid: str, latitude: float, longitude: float) -> None:
        state = self._load()
        state["assets"][uuid]["location"] = [latitude, longitude]
        self._save(state)


def _blank(files: list[str]) -> dict[str, Any]:
    return {
        "files": files, "favorite": False, "description": None, "keywords": [], "date": None,
        "location": None,
    }  # fmt: skip
