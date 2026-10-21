"""The real thing: import the fixtures into a throwaway library open in Photos.app.

Opt-in, because it drives Photos.app, which acts on whatever library it has open: Delmar
creates an empty library under /tmp (Photos > hold Option at launch > Create New; Cmd-Shift-G
in the dialog to type /tmp), opens it, gives Terminal Full Disk Access (to read which library
Photos has open), then runs, in Terminal at the Mac (Automation asks there):

    cd ~/Code/photo-library-migration
    PHOTOMIGRATE_PHOTOS_APP_LIBRARY=/tmp/photomigrate-test.photoslibrary \\
        uv run pytest tests/macos/test_photos_app_import.py -m macos --no-cov

The importer refuses to run unless Photos has exactly that library open.
"""

import os
from pathlib import Path

import pytest

from photomigrate.config import ImportConfig
from photomigrate.importer.library import PhotoscriptLibrary
from photomigrate.importer.stage import FROM_GOOGLE, ImportStage
from photomigrate.runner import run_stage
from tests.integration.test_import_stage import ZONE, setup  # noqa: F401

pytestmark = pytest.mark.macos
LIBRARY = os.environ.get("PHOTOMIGRATE_PHOTOS_APP_LIBRARY")


@pytest.mark.skipif(not LIBRARY, reason="needs a throwaway library open in Photos.app")
def test_the_fixtures_import_into_photos_once(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    import osxphotos

    path, takeout, _ = setup
    library = Path(str(LIBRARY))
    common = {"catalogue_path": path, "plan_dir": tmp_path / "plans", "apply": True}
    photos = PhotoscriptLibrary(library)
    with ImportStage(photos, takeout, tmp_path / "work", zone=ZONE, config=ImportConfig()) as stage:
        first = run_stage(stage, **common)  # type: ignore[arg-type]
    with ImportStage(photos, takeout, tmp_path / "work", zone=ZONE, config=ImportConfig()) as stage:
        again = run_stage(stage, **common)  # type: ignore[arg-type]

    assert first.applied >= 1
    assert again.plan.actions == ()
    database = osxphotos.PhotosDB(dbfile=str(library))
    imported = database.photos(albums=[FROM_GOOGLE])
    assert len(imported) == 3
    beach = next(p for p in imported if p.original_filename.endswith("IMG_0001.JPG"))
    assert (beach.favorite, beach.description) == (True, "Plage")
    assert {"People/Ann", "from-google"} <= set(beach.keywords)
    assert "Holiday" in beach.albums
    assert any(p.live_photo for p in imported)
