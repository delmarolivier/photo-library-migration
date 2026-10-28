"""Map tiles: fetched by the gallery once, with a User-Agent, and cached on disk."""

import threading
import urllib.error
from pathlib import Path

import pytest

from photomigrate.review import tiles
from photomigrate.review.tiles import USER_AGENT, Tiles


def test_a_tile_is_fetched_once_and_then_read_from_the_cache(tmp_path: Path) -> None:
    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        return b"png"

    found = Tiles(tmp_path, fetch)

    assert found.get(15, 17_896, 19_829) == b"png"
    assert found.get(15, 17_896, 19_829) == b"png"
    assert asked == ["https://tile.openstreetmap.org/15/17896/19829.png"]
    assert Tiles(tmp_path, fetch).get(15, 17_896, 19_829) == b"png"
    assert len(asked) == 1


@pytest.mark.parametrize(("zoom", "x", "y"), [(20, 0, 0), (-1, 0, 0), (2, 4, 0), (2, 0, -1)])
def test_tiles_that_do_not_exist_are_refused(tmp_path: Path, zoom: int, x: int, y: int) -> None:
    with pytest.raises(ValueError, match="no tile"):
        Tiles(tmp_path, lambda url: b"").get(zoom, x, y)


def test_a_failed_fetch_caches_nothing(tmp_path: Path) -> None:
    def fetch(url: str) -> bytes:
        raise urllib.error.URLError("offline")

    with pytest.raises(OSError, match="offline"):
        Tiles(tmp_path, fetch).get(1, 0, 0)
    assert not list(tmp_path.rglob("*.png"))


def test_fetches_go_one_at_a_time(tmp_path: Path) -> None:
    busy, overlap = threading.Lock(), []

    def fetch(url: str) -> bytes:
        if not busy.acquire(blocking=False):
            overlap.append(url)
            return b"png"
        try:
            threading.Event().wait(0.01)
            return b"png"
        finally:
            busy.release()

    found = Tiles(tmp_path, fetch)
    threads = [threading.Thread(target=found.get, args=(3, n, 0)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert overlap == []


def test_the_fetch_names_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    class Reply:
        def __enter__(self) -> "Reply":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b"png"

    def urlopen(request, timeout):  # type: ignore[no-untyped-def]
        seen["agent"], seen["timeout"] = request.get_header("User-agent"), timeout
        return Reply()

    monkeypatch.setattr(tiles.urllib.request, "urlopen", urlopen)

    assert tiles.fetch("https://tile.openstreetmap.org/1/0/0.png") == b"png"
    assert seen == {"agent": USER_AGENT, "timeout": tiles.TIMEOUT}
    assert "photomigrate" in USER_AGENT
