"""The SQLite catalogue: every item, date candidate, group, decision and applied action.

Stages read and write it through ``Catalogue``. Writes are idempotent: storing the same facts
again changes nothing, timestamps included, so re-running a stage leaves it unchanged.
Dry runs use ``Catalogue.snapshot``, an in-memory copy that never touches the file.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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
ALTER TABLE items ADD COLUMN google_url TEXT;
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

SCHEMA_V4 = """
-- Apple's Live Photo identifier (MakerNotes on the still, a QuickTime key on the motion clip).
ALTER TABLE items ADD COLUMN content_id TEXT;
CREATE INDEX items_content_id ON items (content_id);
"""

SCHEMA_V5 = """
-- Apple's aesthetic scores (JSON, from osxphotos) and the IANA time zone of the capture.
ALTER TABLE items ADD COLUMN scores TEXT NOT NULL DEFAULT '{}';
ALTER TABLE items ADD COLUMN timezone TEXT;
"""

SCHEMA_V6 = """
-- A 256-bit pHash, to tell copies from crops when the 64-bit hashes look alike.
ALTER TABLE items ADD COLUMN phash_fine TEXT;
"""

SCHEMA_V7 = """
-- Videos: pHashes of 8 evenly sampled frames (comma-separated) and the bitrate (bits/s).
ALTER TABLE items ADD COLUMN video_signature TEXT;
ALTER TABLE items ADD COLUMN bitrate INTEGER;
"""

SCHEMA_V8 = """
-- A new group kind, 'edit-pair'. SQLite cannot change a CHECK constraint, so the table is
-- rebuilt (foreign keys are off during migrations and checked afterwards).
CREATE TABLE groups_v8 (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN (
        'duplicate', 'best-shot', 'date-batch', 'live-pair', 'edit-pair'
    )),
    key        TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (kind, key)
);
INSERT INTO groups_v8 (id, kind, key, details, created_at)
    SELECT id, kind, key, details, created_at FROM groups;
DROP TABLE groups;
ALTER TABLE groups_v8 RENAME TO groups;
"""

SCHEMA_V9 = """
-- Quality: raw measurements (JSON, taken at ingest or index), and the flags the quality
-- stage derives from them with the configured thresholds, each with a score from 0 to 1.
ALTER TABLE items ADD COLUMN quality TEXT NOT NULL DEFAULT '{}';
CREATE TABLE quality_flags (
    item_id INTEGER NOT NULL REFERENCES items (id),
    flag    TEXT NOT NULL,
    score   REAL NOT NULL,
    PRIMARY KEY (item_id, flag)
);
"""

SCHEMA_V10 = """
-- Best shot: an open_clip image embedding (float16 bytes, see photomigrate.embedding) and the
-- faces Apple Vision found (JSON, see photomigrate.faces). NULL means not measured.
ALTER TABLE items ADD COLUMN embedding BLOB;
ALTER TABLE items ADD COLUMN faces TEXT;
"""

SCHEMA_V11 = """
-- The camera (make and model) from EXIF or QuickTime, for date batches.
ALTER TABLE items ADD COLUMN camera TEXT;
"""

SCHEMA_V12 = """
-- Clutter: the text Apple Vision found (JSON, see photomigrate.text_regions; NULL means not
-- measured), and the review queue: each cluttered item's category, score and evidence.
ALTER TABLE items ADD COLUMN text TEXT;
CREATE TABLE clutter (
    item_id  INTEGER PRIMARY KEY REFERENCES items (id),
    category TEXT NOT NULL,
    score    REAL NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}'
);
"""

SCHEMA_V13 = """
-- Location backfill: a proposed place for an item without GPS, how it was found, and the
-- evidence. Written to the item only once the review approves it.
CREATE TABLE inferred_locations (
    item_id   INTEGER PRIMARY KEY REFERENCES items (id),
    latitude  REAL NOT NULL,
    longitude REAL NOT NULL,
    method    TEXT NOT NULL,
    evidence  TEXT NOT NULL DEFAULT '{}'
);
"""

SCHEMA_V14 = """
-- Imports into iCloud Photos: each Google item's Photos uuid (a Live Photo's still and clip
-- share one), the SHA-256 of the working copy that was imported, and its batch.
CREATE TABLE imports (
    item_id        INTEGER PRIMARY KEY REFERENCES items (id),
    photos_uuid    TEXT NOT NULL,
    working_sha256 TEXT NOT NULL,
    batch          TEXT NOT NULL,
    imported_at    TEXT NOT NULL
);
"""

SCHEMA_V15 = """
-- The existing iCloud items in the "To Delete" album, and why. Delmar deletes them by hand.
CREATE TABLE staged (
    item_id   INTEGER PRIMARY KEY REFERENCES items (id),
    reason    TEXT NOT NULL,
    staged_at TEXT NOT NULL
);
"""

SCHEMA_V16 = """
-- The size in bytes of the item's original, for the reconciliation report.
ALTER TABLE items ADD COLUMN size INTEGER;
"""

SCHEMA_V17 = """
-- A new group kind, 'highlights': one year's event, its best photos kept (see
-- photomigrate.analyse.highlights). The table is rebuilt as in v8.
CREATE TABLE groups_v17 (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN (
        'duplicate', 'best-shot', 'date-batch', 'live-pair', 'edit-pair', 'highlights'
    )),
    key        TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (kind, key)
);
INSERT INTO groups_v17 (id, kind, key, details, created_at)
    SELECT id, kind, key, details, created_at FROM groups;
DROP TABLE groups;
ALTER TABLE groups_v17 RENAME TO groups;
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial schema", SCHEMA_V1),
    Migration(2, "metadata plans", SCHEMA_V2),
    Migration(3, "albums, copies and keywords", SCHEMA_V3),
    Migration(4, "content identifiers", SCHEMA_V4),
    Migration(5, "scores and time zones", SCHEMA_V5),
    Migration(6, "fine perceptual hashes", SCHEMA_V6),
    Migration(7, "video signatures and bitrates", SCHEMA_V7),
    Migration(8, "edit pairs", SCHEMA_V8),
    Migration(9, "quality", SCHEMA_V9),
    Migration(10, "embeddings and faces", SCHEMA_V10),
    Migration(11, "cameras", SCHEMA_V11),
    Migration(12, "clutter", SCHEMA_V12),
    Migration(13, "inferred locations", SCHEMA_V13),
    Migration(14, "imports", SCHEMA_V14),
    Migration(15, "staged for To Delete", SCHEMA_V15),
    Migration(16, "sizes", SCHEMA_V16),
    Migration(17, "highlights", SCHEMA_V17),
)

GroupKind = Literal["duplicate", "best-shot", "date-batch", "live-pair", "edit-pair", "highlights"]
Subject = Literal["item", "group"]


@dataclass(frozen=True)
class Group:
    id: int
    kind: GroupKind
    key: str
    details: dict[str, Any]


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
    pending = migrations[current:]
    if not pending:
        return len(migrations)
    # Table rebuilds need foreign keys off; they are checked, then restored, afterwards.
    enforced = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for migration in pending:
            try:
                conn.executescript(
                    f"BEGIN;\n{migration.sql}\nPRAGMA user_version = {migration.version};\nCOMMIT;"
                )
            except sqlite3.Error:
                conn.execute("ROLLBACK")  # the script opened the transaction with its first line
                raise
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise sqlite3.IntegrityError(f"foreign keys broken by a migration: {broken[:5]}")
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if enforced else 'OFF'}")
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
    phash_fine: str | None = None
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
    google_url: str | None = None
    content_id: str | None = None
    scores: str = "{}"
    timezone: str | None = None
    video_signature: str | None = None
    bitrate: int | None = None
    quality: str = "{}"
    embedding: bytes | None = None
    faces: str | None = None
    camera: str | None = None
    text: str | None = None
    size: int | None = None
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
        conn = sqlite3.connect(path, isolation_level=None)
        # Write-ahead logging, so reading the catalogue while a stage runs never locks it out.
        conn.execute("PRAGMA journal_mode = WAL")
        return cls(conn, clock=clock)

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

    def names(self) -> dict[int, str]:
        """Every item's ``source:source_key``, by id, without loading the items."""
        rows = self._conn.execute("SELECT id, source, source_key FROM items")
        return {int(row["id"]): f"{row['source']}:{row['source_key']}" for row in rows}

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

    def item(self, item_id: int) -> CatalogueItem:
        row = self._conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(item_id)
        return _from_row(row)

    def add_flags(self, item_id: int, flags: Iterable[str]) -> None:
        """Add flags to an item; nothing is written if it already has them all."""
        current = self.item(item_id).flags
        merged = current | set(flags)
        if merged != current:
            self._conn.execute(
                "UPDATE items SET flags = ?, updated_at = ? WHERE id = ?",
                (json.dumps(sorted(merged)), self.now(), item_id),
            )

    def set_quality_flags(self, item_id: int, flags: Mapping[str, float]) -> None:
        """Make the item's quality flags exactly ``flags`` (flag -> score)."""
        current = self.quality_flags(item_id).get(item_id, {})
        if current == dict(flags):
            return
        self._conn.execute("DELETE FROM quality_flags WHERE item_id = ?", (item_id,))
        self._conn.executemany(
            "INSERT INTO quality_flags (item_id, flag, score) VALUES (?, ?, ?)",
            [(item_id, flag, score) for flag, score in sorted(flags.items())],
        )

    def quality_flags(self, item_id: int | None = None) -> dict[int, dict[str, float]]:
        """item id -> {flag: score}, for every flagged item (or just ``item_id``)."""
        rows = self._conn.execute(
            "SELECT item_id, flag, score FROM quality_flags WHERE ? IS NULL OR item_id = ? "
            "ORDER BY item_id, flag",
            (item_id, item_id),
        )
        found: dict[int, dict[str, float]] = {}
        for row in rows:
            found.setdefault(int(row["item_id"]), {})[row["flag"]] = float(row["score"])
        return found

    def set_clutter(
        self,
        item_id: int,
        category: str | None,
        score: float = 0.0,
        evidence: Mapping[str, float] | None = None,
    ) -> None:
        """Put the item in the clutter queue under ``category``, or take it out (``None``)."""
        if category is None:
            self._conn.execute("DELETE FROM clutter WHERE item_id = ?", (item_id,))
            return
        encoded = json.dumps(dict(evidence or {}), sort_keys=True)
        self._conn.execute(
            "INSERT INTO clutter (item_id, category, score, evidence) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (item_id) DO UPDATE SET category = excluded.category, "
            "score = excluded.score, evidence = excluded.evidence "
            "WHERE clutter.category IS NOT excluded.category OR clutter.score IS NOT "
            "excluded.score OR clutter.evidence IS NOT excluded.evidence",
            (item_id, category, score, encoded),
        )

    def clutter(self, item_id: int | None = None) -> dict[int, tuple[str, float, dict[str, float]]]:
        """item id -> (category, score, evidence), for every queued item (or just ``item_id``)."""
        rows = self._conn.execute(
            "SELECT * FROM clutter WHERE ? IS NULL OR item_id = ? ORDER BY item_id",
            (item_id, item_id),
        )
        return {
            int(row["item_id"]): (row["category"], float(row["score"]), json.loads(row["evidence"]))
            for row in rows
        }

    def set_inferred_location(
        self,
        item_id: int,
        place: tuple[float, float] | None,
        method: str = "",
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        """Propose ``place`` (latitude, longitude) for the item, or withdraw it (``None``)."""
        if place is None:
            self._conn.execute("DELETE FROM inferred_locations WHERE item_id = ?", (item_id,))
            return
        encoded = json.dumps(dict(evidence or {}), sort_keys=True)
        self._conn.execute(
            "INSERT INTO inferred_locations (item_id, latitude, longitude, method, evidence) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (item_id) DO UPDATE SET "
            "latitude = excluded.latitude, longitude = excluded.longitude, "
            "method = excluded.method, evidence = excluded.evidence "
            "WHERE inferred_locations.latitude IS NOT excluded.latitude "
            "OR inferred_locations.longitude IS NOT excluded.longitude "
            "OR inferred_locations.method IS NOT excluded.method "
            "OR inferred_locations.evidence IS NOT excluded.evidence",
            (item_id, place[0], place[1], method, encoded),
        )

    def inferred_locations(self) -> dict[int, tuple[tuple[float, float], str, dict[str, Any]]]:
        """item id -> ((latitude, longitude), method, evidence), for every proposal."""
        rows = self._conn.execute("SELECT * FROM inferred_locations ORDER BY item_id")
        return {
            int(row["item_id"]): (
                (float(row["latitude"]), float(row["longitude"])),
                row["method"],
                json.loads(row["evidence"]),
            )
            for row in rows
        }

    def record_import(self, item_id: int, uuid: str, working_sha256: str, batch: str) -> None:
        """Record that the item is in iCloud Photos as ``uuid`` (recorded once)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO imports (item_id, photos_uuid, working_sha256, batch, "
            "imported_at) VALUES (?, ?, ?, ?, ?)",
            (item_id, uuid, working_sha256, batch, self.now()),
        )

    def own_imports(self) -> set[int]:
        """The iCloud items that are assets the importer made: a re-index brings them back,
        and they are the same pictures as their Google originals."""
        rows = self._conn.execute(
            "SELECT i.id FROM items i JOIN imports m ON m.photos_uuid = i.source_key "
            "WHERE i.source = 'icloud'"
        )
        return {int(row["id"]) for row in rows}

    def imports(self) -> dict[int, tuple[str, str, str]]:
        """item id -> (Photos uuid, working copy SHA-256, batch), for every imported item."""
        rows = self._conn.execute("SELECT * FROM imports ORDER BY item_id")
        return {
            int(row["item_id"]): (row["photos_uuid"], row["working_sha256"], row["batch"])
            for row in rows
        }

    def set_staged(self, item_id: int, reason: str | None) -> None:
        """Record that the item is in the "To Delete" album, and why, or no longer (``None``)."""
        if reason is None:
            self._conn.execute("DELETE FROM staged WHERE item_id = ?", (item_id,))
            return
        self._conn.execute(
            "INSERT INTO staged (item_id, reason, staged_at) VALUES (?, ?, ?) "
            "ON CONFLICT (item_id) DO UPDATE SET reason = excluded.reason "
            "WHERE staged.reason IS NOT excluded.reason",
            (item_id, reason, self.now()),
        )

    def staged(self) -> dict[int, str]:
        rows = self._conn.execute("SELECT item_id, reason FROM staged ORDER BY item_id")
        return {int(row["item_id"]): str(row["reason"]) for row in rows}

    # -- groups -----------------------------------------------------------------------------

    def upsert_group(self, kind: GroupKind, key: str, details: Mapping[str, Any]) -> int:
        encoded = json.dumps(dict(details), sort_keys=True, ensure_ascii=False)
        cursor = self._conn.execute(
            "INSERT INTO groups (kind, key, details, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (kind, key) DO UPDATE SET details = excluded.details "
            "WHERE groups.details IS NOT excluded.details RETURNING id",
            (kind, key, encoded, self.now()),
        )
        returned = cursor.fetchone()
        if returned is None:
            returned = self._conn.execute(
                "SELECT id FROM groups WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
        return int(returned["id"])

    def discard_group(self, kind: GroupKind, key: str) -> None:
        """Remove a proposal group and its member list (catalogue rows only, never media)."""
        row = self._conn.execute(
            "SELECT id FROM groups WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        if row is not None:
            self._conn.execute("DELETE FROM group_members WHERE group_id = ?", (row["id"],))
            self._conn.execute("DELETE FROM groups WHERE id = ?", (row["id"],))

    def groups(self, kind: GroupKind) -> list[Group]:
        rows = self._conn.execute("SELECT * FROM groups WHERE kind = ? ORDER BY id", (kind,))
        return [
            Group(row["id"], row["kind"], row["key"], json.loads(row["details"])) for row in rows
        ]

    def set_group_members(
        self, group_id: int, members: Iterable[tuple[int, str | None, int | None]]
    ) -> None:
        """Make the group's members exactly ``members`` (item id, role, rank)."""
        wanted = set(members)
        have = set(self.group_members(group_id))
        for item_id, _role, _rank in sorted(have - wanted, key=repr):
            self._conn.execute(
                "DELETE FROM group_members WHERE group_id = ? AND item_id = ?", (group_id, item_id)
            )
        for item_id, role, rank in sorted(wanted - have, key=repr):
            self._conn.execute(
                "INSERT OR REPLACE INTO group_members (group_id, item_id, role, rank) "
                "VALUES (?, ?, ?, ?)",
                (group_id, item_id, role, rank),
            )

    def group_members(self, group_id: int) -> list[tuple[int, str | None, int | None]]:
        rows = self._conn.execute(
            "SELECT item_id, role, rank FROM group_members WHERE group_id = ? "
            "ORDER BY rank, item_id",
            (group_id,),
        )
        return [(int(row["item_id"]), row["role"], row["rank"]) for row in rows]

    # -- decisions (the user's answers) ----------------------------------------------------

    def set_decision(
        self, subject: Subject, subject_id: int, kind: str, value: Mapping[str, Any]
    ) -> None:
        """Record the answer about an item or group; a new answer replaces the old one."""
        encoded = json.dumps(dict(value), sort_keys=True, ensure_ascii=False)
        self._conn.execute(
            "INSERT INTO decisions (subject, subject_id, kind, value, decided_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (subject, subject_id, kind) DO UPDATE SET "
            "value = excluded.value, decided_at = excluded.decided_at "
            "WHERE decisions.value IS NOT excluded.value",
            (subject, subject_id, kind, encoded, self.now()),
        )

    def decision(self, subject: Subject, subject_id: int, kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT value FROM decisions WHERE subject = ? AND subject_id = ? AND kind = ?",
            (subject, subject_id, kind),
        ).fetchone()
        return None if row is None else dict(json.loads(row["value"]))

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

    def copies(self, source: Source) -> list[tuple[str, int]]:
        """Every extra Takeout location of a ``source`` item: (path, item id)."""
        rows = self._conn.execute(
            "SELECT source_key, item_id FROM item_copies WHERE source = ? ORDER BY source_key",
            (source,),
        )
        return [(str(row["source_key"]), int(row["item_id"])) for row in rows]

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

    def applied_actions(self, stage: str) -> dict[str, dict[str, int]]:
        """target -> {action key: when it was last applied}, for the stage's applied actions.

        "When" is a number that only grows, so later applications compare greater.
        """
        rows = self._conn.execute(
            "SELECT a.target, a.key, MAX(a.id) AS last FROM actions a "
            "JOIN stage_runs r ON r.id = a.run_id "
            "WHERE r.stage = ? AND a.status = 'applied' GROUP BY a.target, a.key",
            (stage,),
        )
        history: dict[str, dict[str, int]] = {}
        for row in rows:
            history.setdefault(row["target"], {})[row["key"]] = int(row["last"])
        return history

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
