"""The SQLite catalogue: every item, date candidate, group, decision and applied action.

Stages read and write it through ``Catalogue``. Writes are idempotent: storing the same facts
again changes nothing, timestamps included, so re-running a stage leaves it unchanged.
Dry runs use ``Catalogue.snapshot``, an in-memory copy that never touches the file.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
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

MIGRATIONS: tuple[Migration, ...] = (Migration(1, "initial schema", SCHEMA_V1),)


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


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Catalogue:
    """A migrated catalogue connection. Use ``open`` (read-write) or ``snapshot`` (dry runs)."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = _utc_now) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self.version = migrate(conn)
        self._conn = conn
        self._clock = clock
        self._depth = 0

    @classmethod
    def open(cls, path: Path, *, clock: Clock = _utc_now) -> Catalogue:
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(path, isolation_level=None), clock=clock)

    @classmethod
    def snapshot(cls, path: Path, *, clock: Clock = _utc_now) -> Catalogue:
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
