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

from photomigrate.catalogue import Catalogue
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
    with Catalogue.open(path) as catalogue:
        ours = sorted({uuid for uuid, _, _ in catalogue.imports().values()})
    database = osxphotos.PhotosDB(dbfile=str(library))
    imported = database.photos(uuid=ours, movies=True)  # this run's: earlier attempts don't count
    assert len(imported) == len(ours), "every recorded asset is in Photos"
    assert all(FROM_GOOGLE in p.albums for p in imported)
    beach = next(p for p in imported if p.original_filename.endswith("IMG_0001.JPG"))
    assert (beach.favorite, beach.description) == (True, "Plage")
    assert {"People/Ann", "from-google"} <= set(beach.keywords)
    assert "Holiday" in beach.albums
    # The fixture's still has no Apple content identifier (exiftool cannot write MakerNotes),
    # so Photos keeps its clip apart; the importer records the clip as its own asset.
    assert any(p.ismovie and p.original_filename.endswith("IMG_0003.MOV") for p in imported)


@pytest.mark.skipif(not LIBRARY, reason="needs a throwaway library open in Photos.app")
def test_a_real_live_photo_is_joined_into_one_asset(live_library: Path, tmp_path: Path) -> None:
    """A genuine iPhone Live Photo (from osxphotos' test library), imported the way the
    importer does: still and clip together, renamed. Photos must join them."""
    import shutil

    import osxphotos

    source = osxphotos.PhotosDB(dbfile=str(live_library))
    live = next(p for p in source.photos() if p.live_photo and p.path and p.path_live_photo)
    still = tmp_path / f"9001-{live.original_filename}"
    clip = tmp_path / f"9001-{Path(live.original_filename).stem}{Path(live.path_live_photo).suffix}"
    shutil.copyfile(live.path, still)
    shutil.copyfile(live.path_live_photo, clip)

    ((still_uuid, clip_uuid),) = PhotoscriptLibrary(Path(str(LIBRARY))).import_assets(
        [[still, clip]], "photomigrate live test"
    )

    assert still_uuid == clip_uuid, "Photos joined the clip to its still"
    (asset,) = osxphotos.PhotosDB(dbfile=str(LIBRARY)).photos(uuid=[still_uuid])
    assert asset.live_photo
