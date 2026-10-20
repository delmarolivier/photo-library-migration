"""Disposable Photos libraries for macOS-only tests: osxphotos' test libraries, fetched once.

They come from the osxphotos repository at a pinned commit (sparse, blobless fetch), into the
system temp dir (an allowed root for the media guard). Each test gets its own APFS clone.
"""

import fcntl
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

OSXPHOTOS_REPO = "https://github.com/RhetTbull/osxphotos.git"
OSXPHOTOS_COMMIT = "da8dfb212c19c61238dd1a9da8c87fc289324450"  # v0.77.2
LIBRARIES = ("Test-27.0_DevBeta.photoslibrary", "Test-Live-15.7.2.photoslibrary")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _fetch() -> Path:
    root = Path(tempfile.gettempdir()) / "photomigrate-osxphotos" / OSXPHOTOS_COMMIT
    root.mkdir(parents=True, exist_ok=True)
    with (root.parent / f"{OSXPHOTOS_COMMIT}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # xdist workers share one fetch
        if not (root / ".complete").exists():
            _git("init", "-q", cwd=root)
            _git("remote", "add", "origin", OSXPHOTOS_REPO, cwd=root)
            _git(
                "sparse-checkout",
                "set",
                "--no-cone",
                *(f"tests/{lib}" for lib in LIBRARIES),
                cwd=root,
            )
            _git(
                "fetch",
                "-q",
                "--depth",
                "1",
                "--filter=blob:none",
                "origin",
                OSXPHOTOS_COMMIT,
                cwd=root,
            )
            _git("checkout", "-q", "FETCH_HEAD", cwd=root)
            (root / ".complete").touch()
    return root / "tests"


def _clone(source: Path, dest: Path) -> Path:
    if sys.platform == "darwin":  # APFS clone: instant, no extra disk
        subprocess.run(["cp", "-cR", str(source), str(dest)], check=True)
    else:
        shutil.copytree(source, dest)
    return dest


@pytest.fixture(scope="session")
def osxphotos_libraries() -> Path:
    return _fetch()


@pytest.fixture
def test_library(osxphotos_libraries: Path, tmp_path: Path) -> Path:
    return _clone(osxphotos_libraries / LIBRARIES[0], tmp_path / LIBRARIES[0])


@pytest.fixture
def live_library(osxphotos_libraries: Path, tmp_path: Path) -> Path:
    return _clone(osxphotos_libraries / LIBRARIES[1], tmp_path / LIBRARIES[1])
