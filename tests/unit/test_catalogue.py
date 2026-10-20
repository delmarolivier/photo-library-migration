"""The SQLite catalogue: schema, migrations, idempotent writes and read-only snapshots."""

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from photomigrate.catalogue import (
    MIGRATIONS,
    Catalogue,
    CatalogueItem,
    CatalogueVersionError,
    Migration,
    migrate,
)

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def dump(path: Path) -> dict[str, list[tuple[object, ...]]]:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)  # noqa: S608
            for table in tables
        }
    finally:
        conn.close()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "catalogue.sqlite"


@pytest.fixture
def cat(path: Path) -> Iterator[Catalogue]:
    with Catalogue.open(path, clock=Clock()) as catalogue:
        yield catalogue


PHOTO = CatalogueItem(
    source="google",
    source_key="Takeout/Google Photos/Photos from 2019/IMG_1001.JPG",
    kind="photo",
    original_path="work/extracted/Takeout/Google Photos/Photos from 2019/IMG_1001.JPG",
    sha256="ab" * 32,
    width=320,
    height=240,
    latitude=43.6970333,
    longitude=7.266,
    title="IMG_1001.JPG",
    description="Plage à Nice 🏖️",
    favorite=True,
    flags=frozenset({"edited", "people"}),
)


# --- migrations ----------------------------------------------------------------------------------


def test_open_creates_the_file_and_migrates_an_empty_database(path: Path) -> None:
    with Catalogue.open(path) as catalogue:
        assert catalogue.version == MIGRATIONS[-1].version

    assert set(dump(path)) >= {
        "items",
        "date_candidates",
        "groups",
        "group_members",
        "decisions",
        "stage_runs",
        "actions",
    }


def test_reopening_an_up_to_date_catalogue_changes_nothing(path: Path) -> None:
    with Catalogue.open(path, clock=Clock()) as catalogue:
        catalogue.upsert_item(PHOTO)
    before = path.read_bytes()

    with Catalogue.open(path) as catalogue:
        assert catalogue.version == MIGRATIONS[-1].version

    assert path.read_bytes() == before


V1 = Migration(1, "people", "CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL);")
V2 = Migration(2, "nicknames", "ALTER TABLE people ADD COLUMN nickname TEXT;")


def test_migrate_upgrades_an_existing_database_and_keeps_its_data(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "old.sqlite", isolation_level=None)
    assert migrate(conn, (V1,)) == 1
    conn.execute("INSERT INTO people (name) VALUES ('Alice Example')")

    assert migrate(conn, (V1, V2)) == 2

    assert conn.execute("SELECT name, nickname FROM people").fetchall() == [("Alice Example", None)]
    assert conn.execute("PRAGMA user_version").fetchone() == (2,)


def test_a_failing_migration_rolls_back_entirely(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "old.sqlite", isolation_level=None)
    migrate(conn, (V1,))
    broken = Migration(
        2, "broken", "ALTER TABLE people ADD COLUMN nickname TEXT; SELECT nope FROM nowhere;"
    )

    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, (V1, broken))

    assert conn.execute("PRAGMA user_version").fetchone() == (1,)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(people)")]
    assert columns == ["id", "name"]


def test_a_catalogue_newer_than_the_code_is_refused(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "new.sqlite", isolation_level=None)
    migrate(conn, (V1, V2))

    with pytest.raises(CatalogueVersionError, match="newer"):
        migrate(conn, (V1,))


def test_migrations_must_be_numbered_one_by_one(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)

    with pytest.raises(ValueError, match=re.escape("got [1, 3]")):
        migrate(conn, (V1, Migration(3, "gap", "SELECT 1;")))


def test_shipped_migrations_are_numbered_one_by_one() -> None:
    assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))


# --- schema rules --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO items (source, source_key, kind, created_at, updated_at)"
        " VALUES ('flickr', 'k', 'photo', 't', 't')",
        "INSERT INTO items (source, source_key, kind, created_at, updated_at)"
        " VALUES ('google', 'k', 'audio', 't', 't')",
        "INSERT INTO groups (kind, key, created_at) VALUES ('random', 'k', 't')",
        "INSERT INTO date_candidates (item_id, source, value) VALUES (999, 'exif', 't')",
    ],
)
def test_schema_rejects_invalid_rows(cat: Catalogue, path: Path, sql: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_group_kinds_cover_duplicates_best_shots_date_batches_and_live_pairs(
    cat: Catalogue, path: Path
) -> None:
    conn = sqlite3.connect(path)
    for kind in ("duplicate", "best-shot", "date-batch", "live-pair"):
        conn.execute("INSERT INTO groups (kind, key, created_at) VALUES (?, 'k', 't')", (kind,))
    conn.commit()

    assert len(dump(path)["groups"]) == 4


# --- items and date candidates -------------------------------------------------------------------


def test_upsert_inserts_and_reads_back_an_item(cat: Catalogue) -> None:
    item_id = cat.upsert_item(PHOTO)

    stored = cat.get_item("google", PHOTO.source_key)

    assert stored is not None
    assert stored.id == item_id
    assert stored == PHOTO.with_id(item_id)
    assert cat.get_item("icloud", PHOTO.source_key) is None


def test_upserting_the_same_item_again_changes_nothing(cat: Catalogue, path: Path) -> None:
    item_id = cat.upsert_item(PHOTO)
    before = dump(path)

    assert cat.upsert_item(PHOTO) == item_id

    assert dump(path) == before


def test_upserting_a_changed_item_updates_it_and_its_timestamp(cat: Catalogue, path: Path) -> None:
    item_id = cat.upsert_item(PHOTO)
    (row,) = dump(path)["items"]

    cat.upsert_item(PHOTO.replace(description="Nice 🌊", flags=frozenset({"edited"})))

    (updated,) = dump(path)["items"]
    assert updated != row
    stored = cat.get_item("google", PHOTO.source_key)
    assert stored is not None
    assert stored.id == item_id
    assert stored.description == "Nice 🌊"
    assert stored.flags == frozenset({"edited"})


def test_items_lists_by_source(cat: Catalogue) -> None:
    cat.upsert_item(PHOTO)
    cat.upsert_item(CatalogueItem(source="icloud", source_key="UUID-1", kind="video", duration=2.5))

    assert [i.source_key for i in cat.items()] == [PHOTO.source_key, "UUID-1"]
    assert [i.source_key for i in cat.items("icloud")] == ["UUID-1"]


def test_date_candidates_keep_one_value_per_source(cat: Catalogue, path: Path) -> None:
    item_id = cat.upsert_item(PHOTO)
    taken = datetime(2019, 7, 1, 12, 34, 56, tzinfo=timezone(timedelta(hours=2)))
    exif_local = datetime(2019, 7, 1, 12, 34, 56)

    cat.set_date_candidate(item_id, "sidecar-taken", taken)
    cat.set_date_candidate(item_id, "exif", exif_local)
    before = dump(path)
    cat.set_date_candidate(item_id, "exif", exif_local)

    assert dump(path) == before
    assert cat.date_candidates(item_id) == {"sidecar-taken": taken, "exif": exif_local}
    cat.set_date_candidate(item_id, "exif", datetime(2000, 1, 1))
    assert cat.date_candidates(item_id)["exif"] == datetime(2000, 1, 1)


def test_unknown_date_sources_are_rejected(cat: Catalogue) -> None:
    item_id = cat.upsert_item(PHOTO)

    with pytest.raises(sqlite3.IntegrityError):
        cat.set_date_candidate(item_id, "guess", T0)  # type: ignore[arg-type]


# --- transactions --------------------------------------------------------------------------------


def test_transaction_rolls_back_on_error(cat: Catalogue) -> None:
    def failing() -> None:
        with cat.transaction():
            cat.upsert_item(PHOTO)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        failing()

    assert cat.items() == []


def test_nested_transactions_roll_back_only_the_inner_part(cat: Catalogue) -> None:
    def failing_inner() -> None:
        with cat.transaction():
            cat.upsert_item(CatalogueItem(source="icloud", source_key="UUID-1", kind="photo"))
            raise RuntimeError("boom")

    with cat.transaction():
        cat.upsert_item(PHOTO)
        with pytest.raises(RuntimeError):
            failing_inner()

    assert [i.source_key for i in cat.items()] == [PHOTO.source_key]


# --- snapshots for dry runs ----------------------------------------------------------------------


def test_snapshot_of_a_missing_catalogue_creates_no_file(path: Path) -> None:
    with Catalogue.snapshot(path) as snap:
        snap.upsert_item(PHOTO)
        assert snap.version == MIGRATIONS[-1].version

    assert not path.exists()
    assert not path.parent.exists()


def test_snapshot_sees_the_data_but_never_writes_the_file(path: Path) -> None:
    with Catalogue.open(path) as catalogue:
        catalogue.upsert_item(PHOTO)
    before = path.read_bytes()

    with Catalogue.snapshot(path) as snap:
        assert snap.get_item("google", PHOTO.source_key) is not None
        snap.upsert_item(CatalogueItem(source="icloud", source_key="UUID-1", kind="photo"))
        assert len(snap.items()) == 2

    assert path.read_bytes() == before


def test_snapshot_migrates_an_old_catalogue_in_memory_only(tmp_path: Path) -> None:
    old = tmp_path / "old.sqlite"
    conn = sqlite3.connect(old)
    conn.execute("PRAGMA user_version = 0")
    conn.close()
    before = old.read_bytes()

    with Catalogue.snapshot(old) as snap:
        assert snap.version == MIGRATIONS[-1].version

    assert old.read_bytes() == before


def test_open_rejects_naive_clock_values(path: Path) -> None:
    with (
        Catalogue.open(path, clock=lambda: datetime(2026, 1, 1)) as catalogue,
        pytest.raises(ValueError, match="timezone"),
    ):
        catalogue.upsert_item(PHOTO)


def test_v2_upgrades_a_v1_catalogue_and_adds_metadata_plans(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(path, isolation_level=None)
    migrate(conn, MIGRATIONS[:1])
    conn.execute(
        "INSERT INTO items (source, source_key, kind, created_at, updated_at)"
        " VALUES ('google', 'Takeout/x.jpg', 'photo', 't', 't')"
    )
    conn.close()

    with Catalogue.open(path) as catalogue:
        item = catalogue.get_item("google", "Takeout/x.jpg")

    assert item is not None
    assert item.metadata_plan == "{}"


def test_metadata_plans_are_stored_with_the_item(cat: Catalogue) -> None:
    plan = '{"description": "Plage", "taken": "2019-07-01T12:34:56+02:00"}'

    cat.upsert_item(PHOTO.replace(metadata_plan=plan))

    stored = cat.get_item("google", PHOTO.source_key)
    assert stored is not None
    assert stored.metadata_plan == plan
