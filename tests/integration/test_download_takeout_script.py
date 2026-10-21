"""scripts/download-takeout.sh against a local stand-in for Google's Takeout downloads.

The stand-in serves fake zip parts with Google's headers (Content-Disposition, byte ranges),
answers a request without the right cookie with a sign-in page, as Google does, and can drop
a connection halfway to test resuming.
"""

import hashlib
import http.server
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "download-takeout.sh"
COOKIE = "SID=delmar-session"
NAMES = [f"takeout-20261020T084111Z-{n:03d}.zip" for n in (1, 2, 3)]

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")


def part(n: int) -> bytes:
    return b"PK\x03\x04" + bytes([n]) * (200_000 + n)


class Google(http.server.BaseHTTPRequestHandler):
    parts: ClassVar[dict[str, bytes]] = {}
    cut_once: ClassVar[set[str]] = set()
    claimed_size: ClassVar[int | None] = None

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if COOKIE not in (self.headers.get("Cookie") or ""):
            body = b"<html><title>Sign in - Google Accounts</title></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        name = re.sub(r".*i=", "", self.path)
        data = self.parts[name]
        start, end = 0, len(data) - 1
        ranged = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        if ranged:
            start = int(ranged[1])
            end = int(ranged[2]) if ranged[2] else end
            self.send_response(206)
            total = self.claimed_size or len(data)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        else:
            self.send_response(200)
        chunk = data[start : end + 1]
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        if name in self.cut_once and not ranged:
            self.cut_once.discard(name)
            self.wfile.write(chunk[: len(chunk) // 2])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(chunk)


@pytest.fixture
def google() -> Iterator[str]:
    Google.parts = {name: part(n) for n, name in enumerate(NAMES, start=1)}
    Google.cut_once = set()
    Google.claimed_size = None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Google)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def places(tmp_path: Path, google: str) -> dict[str, Path]:
    urls = tmp_path / "urls.txt"
    urls.write_text(
        "# the 12 links from takeout.google.com\n"
        + "".join(f"{google}/takeout/download?j=job&i={name}\n" for name in NAMES)
        + "\n",
        encoding="utf-8",
    )
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(f"Cookie: {COOKIE}\n", encoding="utf-8")
    (tmp_path / "drive").mkdir()
    return {
        "urls": urls,
        "cookies": cookies,
        "dest": tmp_path / "drive",
        "scratch": tmp_path / "scratch",
    }


def run(places: dict[str, Path], *extra: str) -> subprocess.CompletedProcess[str]:
    args = ["--urls", str(places["urls"]), "--cookies", str(places["cookies"]),
            "--dest", str(places["dest"]), "--scratch", str(places["scratch"]), *extra]  # fmt: skip
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=120, check=False
    )


def test_every_part_lands_on_the_drive_verified_and_the_scratch_copy_goes(
    places: dict[str, Path],
) -> None:
    result = run(places)

    assert result.returncode == 0, result.stdout + result.stderr
    for n, name in enumerate(NAMES, start=1):
        on_drive = places["dest"] / name
        assert on_drive.read_bytes() == part(n)
        digest = hashlib.sha256(part(n)).hexdigest()
        assert (places["dest"] / f"{name}.sha256").read_text().split()[0] == digest
    assert list(places["scratch"].iterdir()) == []
    assert "[1/3]" in result.stdout
    assert "[3/3]" in result.stdout
    assert "All 3 parts are on" in result.stdout


def test_parts_already_on_the_drive_are_skipped(places: dict[str, Path]) -> None:
    run(places)
    stamps = {name: (places["dest"] / name).stat().st_mtime_ns for name in NAMES}

    again = run(places)

    assert again.returncode == 0, again.stderr
    assert again.stdout.count("already on the drive") == 3
    assert {name: (places["dest"] / name).stat().st_mtime_ns for name in NAMES} == stamps


def test_a_part_damaged_on_the_drive_is_fetched_again(places: dict[str, Path]) -> None:
    run(places)
    (places["dest"] / NAMES[1]).write_bytes(b"PK broken")

    again = run(places)

    assert again.returncode == 0, again.stderr
    assert (places["dest"] / NAMES[1]).read_bytes() == part(2)


def test_a_dropped_download_resumes_where_it_stopped(places: dict[str, Path]) -> None:
    Google.cut_once = {NAMES[0]}

    result = run(places)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (places["dest"] / NAMES[0]).read_bytes() == part(1)


def test_without_a_signed_in_session_it_stops_and_says_so(places: dict[str, Path]) -> None:
    places["cookies"].write_text("Cookie: SID=expired\n", encoding="utf-8")

    result = run(places)

    assert result.returncode != 0
    assert "sign in" in (result.stdout + result.stderr).lower()
    assert list(places["dest"].iterdir()) == []


def test_netscape_cookie_files_from_a_browser_extension_work(places: dict[str, Path]) -> None:
    name, value = COOKIE.split("=")
    places["cookies"].write_text(
        f"# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t2147483647\t{name}\t{value}\n",
        encoding="utf-8",
    )

    result = run(places)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_part_bigger_than_the_free_space_is_not_started(places: dict[str, Path]) -> None:
    Google.claimed_size = 10**18

    result = run(places)

    assert result.returncode != 0
    assert "not enough free space" in result.stderr
    assert list(places["dest"].iterdir()) == []


def test_missing_arguments_print_how_to_use_it() -> None:
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, check=False)

    assert result.returncode == 2
    assert "--urls" in result.stderr
    assert os.access(SCRIPT, os.X_OK), "the script is executable"
