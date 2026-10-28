"""The review gallery: queues, decisions written at once, thumbnails, and localhost only."""

import json
import shutil
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageFilter
from typer.testing import CliRunner

from photomigrate.analyse.best_shot import BestShotStage
from photomigrate.analyse.clutter import ClutterStage
from photomigrate.analyse.date_audit import DateAuditStage
from photomigrate.analyse.dedupe_photo import DedupeStage
from photomigrate.analyse.edited import EditedVersionsStage
from photomigrate.analyse.highlights import HighlightsStage
from photomigrate.analyse.locations import LocationStage
from photomigrate.analyse.quality import QualityStage
from photomigrate.catalogue import Catalogue
from photomigrate.cli import app as cli
from photomigrate.config import (
    BestShotConfig,
    BestShotWeights,
    ClutterConfig,
    Config,
    HighlightsConfig,
    LocationConfig,
)
from photomigrate.ingest.livephotos import LivePhotoStage
from photomigrate.ingest.stage import IngestStage
from photomigrate.review import app as review
from photomigrate.review.app import QUEUES, create_app, serve
from photomigrate.runner import run_stage
from photomigrate.testing.embedder import ThumbnailEmbedder
from photomigrate.testing.media import scene_frames, synthetic_image, synthetic_photo
from photomigrate.testing.takeout import Geo, TakeoutBuilder

FOLDER = "Photos from 2021"
ROOT = f"Takeout/Google Photos/{FOLDER}"
T = datetime(2021, 6, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 20, tzinfo=UTC)
HOST = "http://127.0.0.1"


def at(seconds: float) -> datetime:
    return T + timedelta(seconds=seconds)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A catalogue with something in every queue, and the Takeout it came from."""
    tmp = tmp_path_factory.mktemp("review")
    b = TakeoutBuilder(seed=12)
    first = b.photo(FOLDER, "IMG_1001.JPG", taken=at(0), image=synthetic_photo(30))
    b.photo(FOLDER, "IMG_1002.JPG", taken=at(1), image=synthetic_photo(30, jitter=2))
    b.photo(FOLDER, "IMG_1003.JPG", taken=at(2), image=synthetic_photo(30, jitter=8))
    b.near_duplicate(first, FOLDER, "IMG_1001_small.jpg", kind="resized")
    b.edited(b.photo(FOLDER, "IMG_2001.JPG", taken=at(600), image=synthetic_photo(40)))
    b.photo(FOLDER, "IMG_3001.JPG", taken=at(900), exif_taken=None, sidecar=None)
    b.photo(
        FOLDER, "Screenshot_20210601-120000.png", taken=at(1000), camera=None, exif_taken=None,
        size=(750, 1334),
    )  # fmt: skip
    blurred = synthetic_photo(50).filter(ImageFilter.GaussianBlur(6))
    b.photo(FOLDER, "IMG_4001.JPG", taken=at(2000), image=blurred)
    b.photo(FOLDER, "IMG_5001.JPG", taken=at(3000), geo=Geo(48.85, 2.35), geo_in_exif=True)
    b.photo(FOLDER, "IMG_5002.JPG", taken=at(3300), image=synthetic_photo(70))
    b.photo(FOLDER, "IMG_6001.HEIC", taken=at(4000), image=synthetic_image(80))
    b.video(FOLDER, "VID_7001.mp4", taken=at(5000), frames=scene_frames(3, seconds=2.0))
    # a pair of similar shots: a best-shot group that keeps both, so it asks nothing
    b.photo(FOLDER, "IMG_8001.JPG", taken=at(9000), image=synthetic_photo(90))
    b.photo(FOLDER, "IMG_8002.JPG", taken=at(9001), image=synthetic_photo(90, jitter=3))
    b.write_zips(tmp / "takeout")
    path = tmp / "catalogue.sqlite"
    common = {"catalogue_path": path, "plan_dir": tmp / "plans", "apply": True}
    with IngestStage(
        tmp / "takeout", tmp / "work", zone=ZoneInfo("UTC"), embedder=ThumbnailEmbedder()
    ) as stage:
        run_stage(stage, **common)  # type: ignore[arg-type]
    for step in (
        LivePhotoStage(), EditedVersionsStage(), DedupeStage(), QualityStage(Config().quality),
        BestShotStage(BestShotConfig(min_similarity=0.9)), ClutterStage(ClutterConfig()),
        HighlightsStage(HighlightsConfig(), BestShotWeights(), appeal=None),
        LocationStage(LocationConfig()), DateAuditStage(now=NOW),
    ):  # fmt: skip
        run_stage(step, **common)  # type: ignore[arg-type]
    return path, tmp / "takeout"


@pytest.fixture
def catalogue_path(built: tuple[Path, Path], tmp_path: Path) -> Path:
    copy = tmp_path / "catalogue.sqlite"
    shutil.copy(built[0], copy)
    return copy


@pytest.fixture
def client(catalogue_path: Path, built: tuple[Path, Path], tmp_path: Path) -> TestClient:
    application = create_app(catalogue_path, thumbs=tmp_path / "thumbs", takeout=built[1])
    return TestClient(application, base_url=HOST, headers={"Origin": HOST})


def card(catalogue_path: Path, queue: str) -> dict[str, object]:
    """The first card of a queue, as the app sees it."""
    with Catalogue.open(catalogue_path) as catalogue:
        found = QUEUES[queue].cards(catalogue)
    assert found, f"the fixture has nothing in the {queue} queue"
    return {"subject": found[0].subject, "subject_id": found[0].subject_id, "kind": found[0].kind}


def total(catalogue_path: Path, queue: str) -> int:
    with Catalogue.open(catalogue_path) as catalogue:
        return len(QUEUES[queue].cards(catalogue))


def decision(catalogue_path: Path, subject: str, subject_id: int, kind: str) -> object:
    with Catalogue.open(catalogue_path) as catalogue:
        return catalogue.decision(subject, subject_id, kind)  # type: ignore[arg-type]


# --- pages ---------------------------------------------------------------------------------


def test_the_home_page_lists_every_queue_with_its_progress(client: TestClient) -> None:
    page = client.get("/")

    assert page.status_code == 200
    for queue in QUEUES.values():
        assert queue.title in page.text
    assert "0 /" in page.text
    assert "<progress" in page.text


@pytest.mark.parametrize("name", sorted(QUEUES))
def test_every_queue_has_cards_with_thumbnails(client: TestClient, name: str) -> None:
    page = client.get(f"/queue/{name}")

    assert page.status_code == 200
    assert 'src="/thumb/' in page.text
    assert 'data-key="a"' in page.text, "every card can be accepted from the keyboard"


def test_an_unknown_queue_is_not_found(client: TestClient) -> None:
    assert client.get("/queue/nope").status_code == 404


def test_pages_beyond_the_last_are_empty(client: TestClient) -> None:
    page = client.get("/queue/quality?page=99")

    assert page.status_code == 200
    assert 'src="/thumb/' not in page.text


def test_the_locations_queue_shows_a_small_map(client: TestClient) -> None:
    page = client.get("/queue/locations")

    assert "tile.openstreetmap.org/15/" in page.text
    assert "openstreetmap.org/?mlat=" in page.text


# --- decisions -----------------------------------------------------------------------------


def test_a_decision_is_written_at_once_and_counted(
    client: TestClient, catalogue_path: Path
) -> None:
    target = card(catalogue_path, "quality")

    reply = client.post("/decide", data={**target, "value": json.dumps({"reject": True})})

    assert reply.status_code == 200
    assert "decided" in reply.text
    assert decision(catalogue_path, **target) == {"reject": True}  # type: ignore[arg-type]
    assert f"1 / {total(catalogue_path, 'quality')}" in client.get("/").text


def test_decisions_survive_a_restart(
    client: TestClient, catalogue_path: Path, built: tuple[Path, Path], tmp_path: Path
) -> None:
    target = card(catalogue_path, "locations")
    client.post("/decide", data={**target, "value": json.dumps({"approve": True})})

    again = TestClient(
        create_app(catalogue_path, thumbs=tmp_path / "thumbs", takeout=built[1]),
        base_url=HOST,
    )

    assert f"1 / {total(catalogue_path, 'locations')}" in again.get("/").text
    assert "decided" in again.get("/queue/locations").text


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"kind": "launch"}, 400),
        ({"value": "{not json"}, 400),
        ({"value": "[1, 2]"}, 400),
        ({"subject": "album"}, 400),
        ({"subject_id": "999999"}, 404),
    ],
)
def test_bad_decisions_are_refused(
    client: TestClient, catalogue_path: Path, change: dict[str, str], status: int
) -> None:
    target = card(catalogue_path, "quality")

    reply = client.post("/decide", data={**target, "value": '{"reject": true}', **change})

    assert reply.status_code == status
    assert decision(catalogue_path, **target) is None  # type: ignore[arg-type]


def test_a_group_decision_needs_a_group_that_exists(client: TestClient) -> None:
    reply = client.post(
        "/decide", data={"subject": "group", "subject_id": "999999", "kind": "duplicate",
                         "value": '{"reject": true}'},
    )  # fmt: skip

    assert reply.status_code == 404


@pytest.mark.parametrize(
    ("form", "stored"),
    [
        ({"answer": "year", "year": "2004"}, {"answer": "year", "year": 2004}),
        (
            {"answer": "date", "date": "2019-07-01T12:30"},
            {"answer": "date", "date": "2019-07-01T12:30"},
        ),
        ({"answer": "shift", "offset_hours": "-1.5"}, {"answer": "shift", "offset_seconds": -5400}),
        ({"answer": "accept"}, {"answer": "accept"}),
        ({"answer": "keep"}, {"answer": "keep"}),
        ({"answer": "remove"}, {"answer": "remove"}),
    ],
)
def test_date_answers_from_the_batch_form(
    client: TestClient, catalogue_path: Path, form: dict[str, str], stored: dict[str, object]
) -> None:
    target = card(catalogue_path, "dates")

    reply = client.post("/decide/date", data={"subject_id": target["subject_id"], **form})

    assert reply.status_code == 200, reply.text
    assert decision(catalogue_path, "group", target["subject_id"], "date") == stored  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "form",
    [{"answer": "year", "year": "soon"}, {"answer": "shift", "offset_hours": "x"},
     {"answer": "guess"}, {"answer": "year", "year": "1800"}],
)  # fmt: skip
def test_date_answers_that_cannot_work_are_refused(
    client: TestClient, catalogue_path: Path, form: dict[str, str]
) -> None:
    target = card(catalogue_path, "dates")

    reply = client.post("/decide/date", data={"subject_id": target["subject_id"], **form})

    assert reply.status_code == 400


def test_best_shot_picks_are_any_members_but_at_least_one(
    client: TestClient, catalogue_path: Path
) -> None:
    target = card(catalogue_path, "best-shot")
    with Catalogue.open(catalogue_path) as catalogue:
        group = next(g for g in catalogue.groups("best-shot") if g.id == target["subject_id"])
    names = sorted(group.details["scores"])

    picked = client.post(
        "/decide/best-shot", data={"subject_id": target["subject_id"], "keep": names[:1]}
    )
    stranger = client.post(
        "/decide/best-shot", data={"subject_id": target["subject_id"], "keep": ["google:nope"]}
    )
    nothing = client.post("/decide/best-shot", data={"subject_id": target["subject_id"]})

    assert picked.status_code == 200
    assert decision(catalogue_path, "group", target["subject_id"], "best-shot") == {
        "keep": names[:1]
    }  # type: ignore[arg-type]
    assert (stranger.status_code, nothing.status_code) == (400, 400)
    missing = client.post("/decide/best-shot", data={"subject_id": "999999", "keep": names[:1]})
    assert missing.status_code == 404


def test_a_years_highlights_show_the_picks_and_may_keep_none(
    client: TestClient, catalogue_path: Path
) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        (shown,) = QUEUES["highlights"].cards(catalogue)
        (group,) = catalogue.groups("highlights")
    assert shown.form == "best-shot"
    assert shown.title.startswith("2021 photos: ")
    assert shown.proposal.value == {"keep": group.details["keep"]}
    page = client.get("/queue/highlights")
    assert page.status_code == 200
    assert 'name="kind" value="highlights"' in page.text

    names = sorted(group.details["scores"])
    picked = client.post(
        "/decide/best-shot",
        data={"subject_id": group.id, "kind": "highlights", "keep": names[-1:]},
    )
    assert picked.status_code == 200
    assert decision(catalogue_path, "group", group.id, "highlights") == {"keep": names[-1:]}
    none = client.post("/decide/best-shot", data={"subject_id": group.id, "kind": "highlights"})
    assert none.status_code == 200
    assert decision(catalogue_path, "group", group.id, "highlights") == {"keep": []}
    wrong = client.post("/decide/best-shot", data={"subject_id": group.id, "kind": "clutter"})
    assert wrong.status_code == 400


def test_bulk_decisions_cover_a_whole_category(client: TestClient, catalogue_path: Path) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        ids = [str(i) for i in catalogue.clutter()]

    reply = client.post(
        "/bulk", data={"subject": "item", "kind": "clutter", "ids": ",".join(ids),
                       "value": '{"reject": true}'},
    )  # fmt: skip

    assert reply.status_code == 303 or reply.status_code == 200
    assert all(decision(catalogue_path, "item", int(i), "clutter") == {"reject": True} for i in ids)


def test_bulk_decisions_are_checked_like_single_ones(client: TestClient) -> None:
    reply = client.post(
        "/bulk", data={"subject": "item", "kind": "clutter", "ids": "999999", "value": "{}"}
    )

    assert reply.status_code == 404


def test_reviewing_records_decisions_and_changes_nothing_else(
    client: TestClient, catalogue_path: Path
) -> None:
    def everything_but_decisions() -> dict[str, list[tuple[object, ...]]]:
        with sqlite3.connect(catalogue_path) as conn:
            tables = [
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ]
            return {
                t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr)  # noqa: S608
                for t in tables
                if t != "decisions"
            }

    before = everything_but_decisions()
    for name in QUEUES:
        client.get(f"/queue/{name}")
        target = card(catalogue_path, name)
        client.post("/decide", data={**target, "value": '{"reject": true}'})

    assert everything_but_decisions() == before


# --- thumbnails ----------------------------------------------------------------------------


def item_id(catalogue_path: Path, name: str) -> int:
    with Catalogue.open(catalogue_path) as catalogue:
        found = catalogue.item_for_key("google", f"{ROOT}/{name}")
    assert found is not None
    return found


@pytest.mark.parametrize("name", ["IMG_1001.JPG", "IMG_6001.HEIC", "VID_7001.mp4"])
def test_thumbnails_of_photos_heic_and_video_posters(
    client: TestClient, catalogue_path: Path, tmp_path: Path, name: str
) -> None:
    reply = client.get(f"/thumb/{item_id(catalogue_path, name)}")

    assert reply.status_code == 200
    assert reply.headers["content-type"] == "image/jpeg"
    from io import BytesIO

    thumb = Image.open(BytesIO(reply.content))
    assert max(thumb.size) <= 320
    assert list((tmp_path / "thumbs").rglob("*.jpg")), "cached on disk"


def test_a_cached_thumbnail_is_not_made_again(
    client: TestClient, catalogue_path: Path, tmp_path: Path
) -> None:
    target = item_id(catalogue_path, "IMG_1001.JPG")
    client.get(f"/thumb/{target}")
    (cached,) = (tmp_path / "thumbs").rglob("*.jpg")
    stamp = cached.stat().st_mtime_ns

    time.sleep(0.01)
    client.get(f"/thumb/{target}")

    assert cached.stat().st_mtime_ns == stamp


def test_without_the_takeout_a_placeholder_is_shown(catalogue_path: Path, tmp_path: Path) -> None:
    client = TestClient(create_app(catalogue_path, thumbs=tmp_path / "t"), base_url=HOST)

    reply = client.get(f"/thumb/{item_id(catalogue_path, 'IMG_1001.JPG')}")

    assert reply.status_code == 200
    assert reply.headers["content-type"] == "image/jpeg"
    assert not list((tmp_path / "t").rglob("*.jpg")), "placeholders are not cached"


def test_icloud_thumbnails_come_from_the_library(catalogue_path: Path, tmp_path: Path) -> None:
    library = tmp_path / "Library.photoslibrary"
    (library / "originals" / "A").mkdir(parents=True)
    synthetic_image(9).save(library / "originals" / "A" / "UUID-9.jpeg")
    with Catalogue.open(catalogue_path) as catalogue:
        from photomigrate.catalogue import CatalogueItem

        uuid = catalogue.upsert_item(
            CatalogueItem(source="icloud", source_key="UUID-9", kind="photo",
                          original_path="originals/A/UUID-9.jpeg", sha256="9" * 64)
        )  # fmt: skip
    client = TestClient(
        create_app(catalogue_path, thumbs=tmp_path / "t", library=library), base_url=HOST
    )

    reply = client.get(f"/thumb/{uuid}")

    assert reply.status_code == 200
    assert list((tmp_path / "t").rglob("*.jpg"))


def test_an_unknown_item_has_no_thumbnail(client: TestClient) -> None:
    assert client.get("/thumb/999999").status_code == 404


def test_the_scripts_and_styles_are_served(client: TestClient) -> None:
    for name in ("htmx.min.js", "review.js", "review.css"):
        assert client.get(f"/static/{name}").status_code == 200
    assert "keydown" in client.get("/static/review.js").text


# --- localhost only ------------------------------------------------------------------------


def test_requests_for_another_host_name_are_refused(client: TestClient) -> None:
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


@pytest.mark.parametrize("origin", [None, "http://evil.example", "null"])
def test_decisions_from_other_sites_are_refused(
    catalogue_path: Path, built: tuple[Path, Path], tmp_path: Path, origin: str | None
) -> None:
    client = TestClient(create_app(catalogue_path, thumbs=tmp_path / "t"), base_url=HOST)
    target = card(catalogue_path, "quality")
    headers = {} if origin is None else {"Origin": origin}

    reply = client.post("/decide", data={**target, "value": '{"reject": true}'}, headers=headers)

    assert reply.status_code == 403
    assert decision(catalogue_path, **target) is None  # type: ignore[arg-type]


def test_the_server_listens_on_localhost_only(catalogue_path: Path, tmp_path: Path) -> None:
    import uvicorn

    server = uvicorn.Server(review.server_config(create_app(catalogue_path, thumbs=tmp_path), 0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        (address,) = {s.getsockname()[0] for srv in server.servers for s in srv.sockets}
        assert address == "127.0.0.1"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_serve_always_binds_to_localhost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    class Recorder:
        def __init__(self, config: object) -> None:
            seen["config"] = config

        def run(self) -> None:
            seen["ran"] = True

    monkeypatch.setattr(review.uvicorn, "Server", Recorder)
    serve(create_app(tmp_path / "c.sqlite", thumbs=tmp_path), port=8765)

    config = seen["config"]
    assert (config.host, config.port, seen["ran"]) == ("127.0.0.1", 8765, True)  # type: ignore[attr-defined]


def test_the_review_command_serves_the_gallery(
    monkeypatch: pytest.MonkeyPatch, catalogue_path: Path, built: tuple[Path, Path], tmp_path: Path
) -> None:
    served: dict[str, object] = {}
    monkeypatch.setattr(
        "photomigrate.cli.serve",
        lambda application, port: served.update(app=application, port=port),
    )
    library = tmp_path / "Library.photoslibrary"
    library.mkdir()
    args = [
        "--catalogue", str(catalogue_path), "review", "--port", "8123", "--takeout",
        str(built[1]), "--library", str(library), "--thumbs", str(tmp_path / "thumbs"),
    ]  # fmt: skip

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert served["port"] == 8123
    assert "http://127.0.0.1:8123" in result.output


def test_a_best_shot_pair_asks_nothing_and_cannot_be_answered(
    client: TestClient, catalogue_path: Path
) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        shown = {c.subject_id for c in QUEUES["best-shot"].cards(catalogue)}
        pair = next(g.id for g in catalogue.groups("best-shot") if g.id not in shown)

    reply = client.post(
        "/decide", data={"subject": "group", "subject_id": str(pair), "kind": "best-shot",
                         "value": '{"keep": []}'},
    )  # fmt: skip

    assert reply.status_code == 404
    assert decision(catalogue_path, "group", pair, "best-shot") is None


@pytest.mark.parametrize(
    ("policy", "kept"), [("pair", 2), ("original-only", 1), ("edited-only", 1)]
)
def test_one_edit_policy_for_every_pair(
    client: TestClient, catalogue_path: Path, policy: str, kept: int
) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        ids = [g.id for g in catalogue.groups("edit-pair")]

    reply = client.post(
        "/bulk", data={"subject": "group", "kind": "edit-pair", "ids": ",".join(map(str, ids)),
                       "value": json.dumps({"policy": policy})},
    )  # fmt: skip

    assert reply.status_code == 200
    answers = [decision(catalogue_path, "group", i, "edit-pair") for i in ids]
    assert all(len(a["keep"]) == kept for a in answers)  # type: ignore[index]


@pytest.mark.parametrize("value", ['{"policy": "both"}', "{nope"])
def test_bulk_answers_that_cannot_work_are_refused(
    client: TestClient, catalogue_path: Path, value: str
) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        ids = [g.id for g in catalogue.groups("edit-pair")]

    reply = client.post(
        "/bulk", data={"subject": "group", "kind": "edit-pair", "ids": ",".join(map(str, ids)),
                       "value": value},
    )  # fmt: skip

    assert reply.status_code == 400
    assert all(decision(catalogue_path, "group", i, "edit-pair") is None for i in ids)


def test_icloud_items_show_their_title(client: TestClient, catalogue_path: Path) -> None:
    from photomigrate.catalogue import CatalogueItem

    with Catalogue.open(catalogue_path) as catalogue:
        uuid = catalogue.upsert_item(
            CatalogueItem(source="icloud", source_key="UUID-Q", kind="photo", title="IMG_0042.HEIC")
        )
        catalogue.set_quality_flags(uuid, {"blurry": 0.5})

    assert "IMG_0042.HEIC" in client.get("/queue/quality").text


def test_the_vendored_htmx_is_the_published_build() -> None:
    import base64
    import hashlib

    script = Path(review.__file__).parent / "static" / "htmx.min.js"
    digest = base64.b64encode(hashlib.sha384(script.read_bytes()).digest()).decode()

    assert digest == "HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+"  # 2.0.4
