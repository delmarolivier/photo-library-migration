"""Map tiles for the locations queue, fetched by the gallery and cached on disk.

OpenStreetMap's tile servers block browsers that send no ``Referer``, and their policy
(https://operations.osmfoundation.org/policies/tiles/) asks an app to name itself in a
``User-Agent``, to cache tiles and to keep to light use. So pages ask the gallery for
``/tile/{zoom}/{x}/{y}.png`` and it fetches each tile at most once, one fetch at a time.
"""

from __future__ import annotations

import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path

USER_AGENT = (
    "photomigrate-review/1.0 (personal photo library review; "
    "+https://github.com/delmarolivier/photo-library-migration)"
)
TIMEOUT = 15.0
MAX_ZOOM = 19

Fetch = Callable[[str], bytes]


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:  # noqa: S310
        data: bytes = reply.read()
    return data


class Tiles:
    def __init__(self, cache: Path, fetch: Fetch = fetch) -> None:
        self.cache = cache
        self._fetch = fetch
        self._lock = threading.Lock()

    def get(self, zoom: int, x: int, y: int) -> bytes:
        """The tile's PNG; raises ``ValueError`` for a tile that does not exist and
        ``OSError`` when it cannot be fetched."""
        if not (0 <= zoom <= MAX_ZOOM and 0 <= x < 2**zoom and 0 <= y < 2**zoom):
            raise ValueError(f"no tile {zoom}/{x}/{y}")
        path = self.cache / str(zoom) / str(x) / f"{y}.png"
        with self._lock:
            if not path.exists():
                data = self._fetch(f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png")
                path.parent.mkdir(parents=True, exist_ok=True)
                partial = path.with_suffix(".part")
                partial.write_bytes(data)
                partial.replace(path)
            return path.read_bytes()
