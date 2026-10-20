"""The SQLite catalogue: every item, date candidate, group, decision and applied action.

Stages read and write it through ``Catalogue``. Writes are idempotent: storing the same facts
again changes nothing, timestamps included, so re-running a stage leaves it unchanged.
Dry runs use ``Catalogue.snapshot``, an in-memory copy that never touches the file.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

Source = Literal["google", "icloud"]
Kind = Literal["photo", "video"]
DateSource = Literal[
    "exif", "quicktime", "sidecar-taken", "sidecar-created", "filename", "mtime", "icloud", "user"
]
Clock = Callable[[], datetime]


class CatalogueVersionError(RuntimeError):
    """The catalogue was written by a newer version of photomigrate."""


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    sql: str


SCHEMA_V1 = """
CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL CHECK (source IN ('google', 'icloud')),
    source_key    TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('photo', 'video')),
    original_path TEXT,
    working_path  TEXT,
    sidecar_path  TEXT,
    sha256        TEXT,
    phash         TEXT,
    dhash         TEXT,
    width         INTEGER,
    height        INTEGER,
    duration      REAL,
    latitude      REAL,
    longitude     REAL,
    altitude      REAL,
    title         TEXT,
    description   TEXT,
    favorite      INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1)),
    flags         TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (source, source_key)
);
CREATE INDEX items_sha256 ON items (sha256);

CREATE TABLE date_candidates (
    item_id INTEGER NOT NULL REFERENCES items (id),
    source  TEXT NOT NULL CHECK (source IN (
        'exif', 'quicktime', 'sidecar-taken', 'sidecar-created', 'filename', 'mtime', 'icloud',
        'user'
    )),
    value   TEXT NOT NULL,
    PRIMARY KEY (item_id, source)
);

CREATE TABLE groups (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('duplicate', 'best-shot', 'date-batch', 'live-pair')),
    key        TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (kind, key)
);

CREATE TABLE group_members (
    group_id INTEGER NOT NULL REFERENCES groups (id),
    item_id  INTEGER NOT NULL REFERENCES items (id),
    role     TEXT,
    rank     INTEGER,
    PRIMARY KEY (group_id, item_id)
);

CREATE TABLE decisions (
    id         INTEGER PRIMARY KEY,
    subject    TEXT NOT NULL CHECK (subject IN ('item', 'group')),
    subject_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE (subject, subject_id, kind)
);

CREATE TABLE stage_runs (
    id          INTEGER PRIMARY KEY,
    stage       TEXT NOT NULL,
    plan_path   TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed'))
);

CREATE TABLE actions (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES stage_runs (id),
    seq        INTEGER NOT NULL,
    key        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    target     TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL CHECK (status IN ('planned', 'applied', 'failed')),
    error      TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, seq)
);
CREATE INDEX actions_key_status ON actions (key, status);
"""

SCHEMA_V2 = """
-- What ingest decided to write into the item's working copy at import (JSON, see
-- photomigrate.ingest.exif.MetadataWrite).
ALTER TABLE items ADD COLUMN metadata_plan TEXT NOT NULL DEFAULT '{}';
"""

SCHEMA_V3 = """
-- Albums, the items in them, extra Takeout locations of one item, and keywords.
CREATE TABLE albums (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL CHECK (source IN ('google', 'icloud')),
    source_key  TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    shared      INTEGER NOT NULL DEFAULT 0 CHECK (shared IN (0, 1)),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (source, source_key)
);

CREATE TABLE album_members (
    album_id INTEGER NOT NULL REFERENCES albums (id),
    item_id  INTEGER NOT NULL REFERENCES items (id),
    PRIMARY KEY (album_id, item_id)
);

CREATE TABLE item_copies (
    source     TEXT NOT NULL CHECK (source IN ('google', 'icloud')),
    source_key TEXT NOT NULL,
    item_id    INTEGER NOT NULL REFERENCES items (id),
    PRIMARY KEY (source, source_key)
);

CREATE TABLE keywords (
    item_id INTEGER NOT NULL REFERENCES items (id),
    keyword TEXT NOT NULL,
    PRIMARY KEY (item_id, keyword)
);
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial schema", SCHEMA_V1),
    Migration(2, "metadata plans", SCHEMA_V2),
    Migration(3, "albums, copies and keywords", SCHEMA_V3),
)


@dataclass(frozen=True)
class Album:
    id: int
    source: Source
    source_key: str
    title: str
    description: str
    shared: bool


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Bring ``conn`` up to the last migration, one transaction per step. Returns the version."""
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise ValueError(f"migrations must be numbered 1, 2, 3… with no gaps; got {versions}")
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(migrations):
        raise CatalogueVersionError(
            f"catalogue schema v{current} is newer than this photomigrate (v{len(migrations)})"
        )
    for migration in migrations[current:]:
        try:
            conn.executescript(
                f"BEGIN;\n{migration.sql}\nPRAGMA user_version = {migration.version};\nCOMMIT;"
            )
        except sqlite3.Error:
            conn.execute("ROLLBACK")  # the script opened the transaction with its first statement
            raise
    return len(migrations)


@dataclass(frozen=True)
class CatalogueItem:
    source: Source
    source_key: str
    kind: Kind
    original_path: str | None = None
    working_path: str | None = None
    sidecar_path: str | None = None
    sha256: str | None = None
    phash: str | None = None
    dhash: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    title: str | None = None
    description: str | None = None
    favorite: bool = False
    flags: frozenset[str] = field(default_factory=frozenset)
    metadata_plan: str = "{}"
    id: int | None = None

    def replace(self, **changes: Any) -> CatalogueItem:
        return dataclasses.replace(self, **changes)

    def with_id(self, item_id: int) -> CatalogueItem:
        return dataclasses.replace(self, id=item_id)


_ITEM_COLUMNS = [f.name for f in dataclasses.fields(CatalogueItem) if f.name != "id"]
_ITEM_UPDATES = [c for c in _ITEM_COLUMNS if c not in ("source", "source_key")]


def _to_row(item: CatalogueItem) -> dict[str, object]:
    row: dict[str, object] = {name: getattr(item, name) for name in _ITEM_COLUMNS}
    row["favorite"] = int(item.favorite)
    row["flags"] = json.dumps(sorted(item.flags))
    return row


def _from_row(row: sqlite3.Row) -> CatalogueItem:
    values = {name: row[name] for name in _ITEM_COLUMNS}
    values["favorite"] = bool(values["favorite"])
    values["flags"] = frozenset(json.loads(values["flags"]))
    return CatalogueItem(**values, id=row["id"])


def utc_now() -> datetime:
    return datetime.now(UTC)


class Catalogue:
    """A migrated catalogue connection. Use ``open`` (read-write) or ``snapshot`` (dry runs)."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self.version = migrate(conn)
        self._conn = conn
        self._clock = clock
        self._depth = 0

    @classmethod
    def open(cls, path: Path, *, clock: Clock = utc_now) -> Catalogue:
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(path, isolation_level=None), clock=clock)

    @classmethod
    def snapshot(cls, path: Path, *, clock: Clock = utc_now) -> Catalogue:
        """An in-memory copy of ``path`` (or an empty catalogue if it does not exist).

        Writes go to memory only, so dry runs can plan against real state without side effects.
        """
        memory = sqlite3.connect(":memory:", isolation_level=None)
        if path.exists():
            source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                source.backup(memory)
            finally:
                source.close()
        return cls(memory, clock=clock)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All-or-nothing writes. Nested use rolls back only the inner block."""
        savepoint = f"sp{self._depth}"
        self._conn.execute("BEGIN IMMEDIATE" if self._depth == 0 else f"SAVEPOINT {savepoint}")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._conn.execute("ROLLBACK")
            else:
                self._conn.execute(f"ROLLBACK TO {savepoint}")
                self._conn.execute(f"RELEASE {savepoint}")
            raise
        self._depth -= 1
        self._conn.execute("COMMIT" if self._depth == 0 else f"RELEASE {savepoint}")

    def now(self) -> str:
        moment = self._clock()
        if moment.tzinfo is None:
            raise ValueError("the catalogue clock must return timezone-aware datetimes")
        return moment.astimezone(UTC).isoformat()

    # -- items -----------------------------------------------------------------------------

    def upsert_item(self, item: CatalogueItem) -> int:
        """Insert or update by (source, source_key). Unchanged items are left untouched."""
        row = _to_row(item)
        now = self.now()
        columns = ", ".join([*_ITEM_COLUMNS, "created_at", "updated_at"])
        placeholders = ", ".join(f":{c}" for c in [*_ITEM_COLUMNS, "created_at", "updated_at"])
        updates = ", ".join(f"{c} = excluded.{c}" for c in [*_ITEM_UPDATES, "updated_at"])
        changed = " OR ".join(f"items.{c} IS NOT excluded.{c}" for c in _ITEM_UPDATES)
        cursor = self._conn.execute(
            f"INSERT INTO items ({columns}) VALUES ({placeholders}) "  # noqa: S608
            f"ON CONFLICT (source, source_key) DO UPDATE SET {updates} WHERE {changed} "
            "RETURNING id",
            {**row, "created_at": now, "updated_at": now},
        )
        returned = cursor.fetchone()  # no row when the update was skipped as unchanged
        if returned is None:
            returned = self._conn.execute(
                "SELECT id FROM items WHERE source = ? AND source_key = ?",
                (item.source, item.source_key),
            ).fetchone()
        return int(returned["id"])

    def get_item(self, source: Source, source_key: str) -> CatalogueItem | None:
        row = self._conn.execute(
            "SELECT * FROM items WHERE source = ? AND source_key = ?", (source, source_key)
        ).fetchone()
        return None if row is None else _from_row(row)

    def items(self, source: Source | None = None) -> list[CatalogueItem]:
        if source is None:
            rows = self._conn.execute("SELECT * FROM items ORDER BY id")
        else:
            rows = self._conn.execute("SELECT * FROM items WHERE source = ? ORDER BY id", (source,))
        return [_from_row(row) for row in rows]

    def set_date_candidate(self, item_id: int, source: DateSource, value: datetime) -> None:
        """Record one date candidate per source. Naive values mean local time, offset unknown."""
        self._conn.execute(
            "INSERT INTO date_candidates (item_id, source, value) VALUES (?, ?, ?) "
            "ON CONFLICT (item_id, source) DO UPDATE SET value = excluded.value "
            "WHERE date_candidates.value IS NOT excluded.value",
            (item_id, source, value.isoformat()),
        )

    def date_candidates(self, item_id: int) -> dict[str, datetime]:
        rows = self._conn.execute(
            "SELECT source, value FROM date_candidates WHERE item_id = ? ORDER BY source",
            (item_id,),
        )
        return {row["source"]: datetime.fromisoformat(row["value"]) for row in rows}

    def find_by_sha256(self, source: Source, sha256: str) -> list[CatalogueItem]:
        rows = self._conn.execute(
            "SELECT * FROM items WHERE source = ? AND sha256 = ? ORDER BY id", (source, sha256)
        )
        return [_from_row(row) for row in rows]

    # -- albums, copies and keywords --------------------------------------------------------

    def upsert_album(
        self,
        source: Source,
        source_key: str,
        title: str,
        description: str = "",
        shared: bool = False,
    ) -> int:
        now = self.now()
        cursor = self._conn.execute(
            "INSERT INTO albums (source, source_key, title, description, shared, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (source, source_key) DO UPDATE "
            "SET title = excluded.title, description = excluded.description, "
            "shared = excluded.shared, updated_at = excluded.updated_at "
            "WHERE albums.title IS NOT excluded.title OR albums.description IS NOT "
            "excluded.description OR albums.shared IS NOT excluded.shared RETURNING id",
            (source, source_key, title, description, int(shared), now, now),
        )
        returned = cursor.fetchone()
        if returned is None:
            returned = self._conn.execute(
                "SELECT id FROM albums WHERE source = ? AND source_key = ?", (source, source_key)
            ).fetchone()
        return int(returned["id"])

    def albums(self) -> list[Album]:
        rows = self._conn.execute("SELECT * FROM albums ORDER BY id")
        return [
            Album(
                id=row["id"],
                source=row["source"],
                source_key=row["source_key"],
                title=row["title"],
                description=row["description"],
                shared=bool(row["shared"]),
            )
            for row in rows
        ]

    def add_album_member(self, album_id: int, item_id: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO album_members (album_id, item_id) VALUES (?, ?)",
            (album_id, item_id),
        )

    def album_members(self, album_id: int) -> list[int]:
        rows = self._conn.execute(
            "SELECT item_id FROM album_members WHERE album_id = ? ORDER BY item_id", (album_id,)
        )
        return [int(row["item_id"]) for row in rows]

    def albums_of(self, item_id: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT a.title FROM albums a JOIN album_members m ON m.album_id = a.id "
            "WHERE m.item_id = ? ORDER BY a.title",
            (item_id,),
        )
        return [str(row["title"]) for row in rows]

    def add_copy(self, source: Source, source_key: str, item_id: int) -> None:
        """Record another Takeout location of ``item_id`` (an album folder's copy of it)."""
        known = self._conn.execute(
            "SELECT item_id FROM item_copies WHERE source = ? AND source_key = ?",
            (source, source_key),
        ).fetchone()
        if known is not None and int(known["item_id"]) != item_id:
            raise ValueError(f"{source_key} is already a copy of item {known['item_id']}")
        self._conn.execute(
            "INSERT OR IGNORE INTO item_copies (source, source_key, item_id) VALUES (?, ?, ?)",
            (source, source_key, item_id),
        )

    def copies_of(self, item_id: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT source_key FROM item_copies WHERE item_id = ? ORDER BY source_key", (item_id,)
        )
        return [str(row["source_key"]) for row in rows]

    def item_for_key(self, source: Source, source_key: str) -> int | None:
        """The item a Takeout path belongs to, whether it is the item's own path or a copy."""
        row = self._conn.execute(
            "SELECT id FROM items WHERE source = ? AND source_key = ? UNION ALL "
            "SELECT item_id FROM item_copies WHERE source = ? AND source_key = ?",
            (source, source_key, source, source_key),
        ).fetchone()
        return None if row is None else int(row[0])

    def set_keywords(self, item_id: int, prefix: str, keywords: Iterable[str]) -> None:
        """Make the item's keywords under ``prefix`` exactly ``keywords`` (others untouched)."""
        wanted = set(keywords)
        if any(not keyword.startswith(prefix) for keyword in wanted):
            raise ValueError(f"every keyword must start with the prefix {prefix!r}")
        have = {k for k in self.keywords(item_id) if k.startswith(prefix)}
        for keyword in sorted(have - wanted):
            self._conn.execute(
                "DELETE FROM keywords WHERE item_id = ? AND keyword = ?", (item_id, keyword)
            )
        for keyword in sorted(wanted - have):
            self._conn.execute(
                "INSERT INTO keywords (item_id, keyword) VALUES (?, ?)", (item_id, keyword)
            )

    def keywords(self, item_id: int) -> set[str]:
        rows = self._conn.execute("SELECT keyword FROM keywords WHERE item_id = ?", (item_id,))
        return {str(row["keyword"]) for row in rows}

    # -- stage runs and the action log ----------------------------------------------------------

    def applied_action_keys(self, stage: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT a.key FROM actions a JOIN stage_runs r ON r.id = a.run_id "
            "WHERE r.stage = ? AND a.status = 'applied'",
            (stage,),
        )
        return {row["key"] for row in rows}

    def start_run(self, stage: str, plan_path: str | None) -> int:
        cursor = self._conn.execute(
            "INSERT INTO stage_runs (stage, plan_path, started_at, status) "
            "VALUES (?, ?, ?, 'running') RETURNING id",
            (stage, plan_path, self.now()),
        )
        return int(cursor.fetchone()["id"])

    def finish_run(self, run_id: int, status: Literal["completed", "failed"]) -> None:
        self._conn.execute(
            "UPDATE stage_runs SET status = ?, finished_at = ? WHERE id = ?",
            (status, self.now(), run_id),
        )

    def log_action(
        self, run_id: int, seq: int, *, key: str, kind: str, target: str, details: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO actions (run_id, seq, key, kind, target, details, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?)",
            (run_id, seq, key, kind, target, details, self.now()),
        )

    def set_action_status(
        self,
        run_id: int,
        seq: int,
        status: Literal["applied", "failed"],
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE actions SET status = ?, error = ?, updated_at = ? WHERE run_id = ? AND seq = ?",
            (status, error, self.now(), run_id, seq),
        )
