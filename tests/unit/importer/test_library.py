"""The Photos library the importer writes to: the real one (photoscript), and a disposable one."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from photomigrate.importer import library as module
from photomigrate.importer.library import LibraryMismatchError, PhotoscriptLibrary
from photomigrate.testing.photos import DisposableLibrary


def touch(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(name.encode())
    return path


# --- the disposable library ----------------------------------------------------------------


def test_a_disposable_library_imports_assets_into_an_album(tmp_path: Path) -> None:
    photos = DisposableLibrary(tmp_path / "Test.photoslibrary")
    still, motion, other = (
        touch(tmp_path, "1-IMG_1.HEIC"),
        touch(tmp_path, "1-IMG_1.MOV"),
        touch(tmp_path, "2-a.jpg"),
    )

    uuids = photos.import_assets([[still, motion], [other]], "From Google")

    assert uuids[0][0] == uuids[0][1], "a Live Photo is one asset"
    assert len({u for asset in uuids for u in asset}) == 2
    reopened = DisposableLibrary(tmp_path / "Test.photoslibrary")
    live = reopened.asset(uuids[0][0])
    assert live["files"] == ["1-IMG_1.HEIC", "1-IMG_1.MOV"]
    assert reopened.albums() == {"From Google": [uuids[0][0], uuids[1][0]]}


def test_a_disposable_library_can_behave_like_photos_with_unpairable_files(
    tmp_path: Path,
) -> None:
    photos = DisposableLibrary(tmp_path / "Test.photoslibrary", pairs_live_photos=False)
    still, clip = touch(tmp_path, "1-IMG_1.HEIC"), touch(tmp_path, "1-IMG_1.MOV")

    ((still_uuid, clip_uuid),) = photos.import_assets([[still, clip]], "From Google")

    assert still_uuid != clip_uuid
    assert photos.asset(clip_uuid)["files"] == ["1-IMG_1.MOV"]
    assert photos.albums()["From Google"] == [still_uuid, clip_uuid]


def test_a_disposable_library_keeps_details_dates_places_and_albums(tmp_path: Path) -> None:
    photos = DisposableLibrary(tmp_path / "Test.photoslibrary")
    ((uuid,),) = photos.import_assets([[touch(tmp_path, "a.jpg")]], "From Google")

    photos.set_details(uuid, favorite=True, description="Nice", keywords=["from-google"])
    photos.set_details(uuid, favorite=True, description=None, keywords=["People/Ann"])
    photos.set_date(uuid, datetime(2004, 7, 1, 12, 0))
    photos.set_location(uuid, 48.85, 2.35)
    photos.add_to_album("Holiday", [uuid])
    photos.add_to_album("Holiday", [uuid])

    asset = DisposableLibrary(tmp_path / "Test.photoslibrary").asset(uuid)
    assert (asset["favorite"], asset["description"]) == (True, "Nice")
    assert asset["keywords"] == ["People/Ann", "from-google"]
    assert (asset["date"], asset["location"]) == ("2004-07-01T12:00:00", [48.85, 2.35])
    assert photos.albums()["Holiday"] == [uuid]


def test_existing_assets_can_be_added_by_hand(tmp_path: Path) -> None:
    photos = DisposableLibrary(tmp_path / "Test.photoslibrary")

    photos.add_existing("UUID-1", "IMG_7001.HEIC")

    assert photos.asset("UUID-1")["files"] == ["IMG_7001.HEIC"]


# --- photoscript ---------------------------------------------------------------------------


class FakePhoto:
    store: ClassVar[dict[str, dict[str, Any]]] = {}

    def __init__(self, uuid: str) -> None:
        self.__dict__["uuid"] = uuid
        FakePhoto.store.setdefault(uuid, {"keywords": [], "filename": uuid})

    def __getattr__(self, name: str) -> Any:
        return FakePhoto.store[self.uuid][name]

    def __setattr__(self, name: str, value: Any) -> None:
        FakePhoto.store[self.uuid][name] = value


class FakeAlbum:
    def __init__(self, name: str) -> None:
        self.name, self.added = name, []  # type: ignore[var-annotated]

    def add(self, photos: list[FakePhoto]) -> None:
        self.added.extend(p.uuid for p in photos)


class FakePhotos:
    def __init__(self) -> None:
        self.albums: dict[str, FakeAlbum] = {}
        self.imports: list[tuple[list[str], str, bool]] = []

    def album(self, name: str) -> FakeAlbum | None:
        return self.albums.get(name)

    def create_album(self, name: str) -> FakeAlbum:
        self.albums[name] = FakeAlbum(name)
        return self.albums[name]

    def import_photos(
        self, paths: list[str], album: FakeAlbum, skip_duplicate_check: bool
    ) -> list[FakePhoto]:
        self.imports.append((paths, album.name, skip_duplicate_check))
        photos = []
        for path in paths:
            name = Path(path).name
            if name.endswith(".MOV") and any(p.endswith(".HEIC") for p in paths):
                continue  # Photos joins a Live Photo's clip to its still
            photo = FakePhoto(f"UUID-{name}")
            photo.filename = name
            photos.append(photo)
        return list(reversed(photos))  # Photos does not promise any order


@pytest.fixture
def photos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakePhotos:
    fake = FakePhotos()
    FakePhoto.store = {}
    monkeypatch.setattr(
        module, "_photoscript", lambda: SimpleNamespace(PhotosLibrary=lambda: fake, Photo=FakePhoto)
    )
    monkeypatch.setattr(module, "_open_library", lambda: str(tmp_path / "Real.photoslibrary"))
    return fake


def test_photoscript_refuses_a_library_photos_does_not_have_open(
    photos: FakePhotos, tmp_path: Path
) -> None:
    with pytest.raises(LibraryMismatchError, match=r"Photos has .*Real\.photoslibrary open"):
        PhotoscriptLibrary(tmp_path / "Other.photoslibrary")


def test_photoscript_imports_into_the_album_and_maps_each_asset_by_file_name(
    photos: FakePhotos, tmp_path: Path
) -> None:
    library = PhotoscriptLibrary(tmp_path / "Real.photoslibrary")
    still, motion, other = (touch(tmp_path, n) for n in ("1-IMG.HEIC", "1-IMG.MOV", "2-a.jpg"))

    uuids = library.import_assets([[still, motion], [other]], "From Google")

    assert uuids == [("UUID-1-IMG.HEIC", "UUID-1-IMG.HEIC"), ("UUID-2-a.jpg",)], "joined clip"
    assert photos.imports == [([str(still), str(motion), str(other)], "From Google", True)]
    library.import_assets([[touch(tmp_path, "3-b.jpg")]], "From Google")
    assert len(photos.albums) == 1, "an existing album is reused"


def test_photoscript_gives_an_unjoined_clip_its_own_uuid(
    photos: FakePhotos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = PhotoscriptLibrary(tmp_path / "Real.photoslibrary")

    def separately(
        paths: list[str], album: FakeAlbum, skip_duplicate_check: bool
    ) -> list[FakePhoto]:
        found = []
        for path in paths:
            photo = FakePhoto(f"UUID-{Path(path).name}")
            photo.filename = Path(path).name
            found.append(photo)
        return found

    monkeypatch.setattr(photos, "import_photos", separately)
    still, clip = touch(tmp_path, "1-IMG.HEIC"), touch(tmp_path, "1-IMG.MOV")

    assert library.import_assets([[still, clip]], "From Google") == [
        ("UUID-1-IMG.HEIC", "UUID-1-IMG.MOV")
    ]


def test_photoscript_says_which_file_photos_did_not_import(
    photos: FakePhotos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = PhotoscriptLibrary(tmp_path / "Real.photoslibrary")
    monkeypatch.setattr(photos, "import_photos", lambda paths, album, skip_duplicate_check: [])

    with pytest.raises(RuntimeError, match=r"did not import 2-a\.jpg"):
        library.import_assets([[touch(tmp_path, "2-a.jpg")]], "From Google")


def test_photoscript_sets_details_dates_places_and_albums(
    photos: FakePhotos, tmp_path: Path
) -> None:
    library = PhotoscriptLibrary(tmp_path / "Real.photoslibrary")
    FakePhoto("U1").keywords = ["People/Ann"]

    library.set_details("U1", favorite=True, description="Nice", keywords=["from-google"])
    library.set_details("U1", favorite=False, description=None, keywords=[])
    library.set_date("U1", datetime(2004, 7, 1, 12, 0, tzinfo=timezone(timedelta(hours=2))))
    library.set_location("U1", 48.85, 2.35)
    library.add_to_album("Holiday", ["U1"])

    stored = FakePhoto.store["U1"]
    assert stored["keywords"] == ["People/Ann", "from-google"]
    assert (stored["favorite"], stored["description"]) == (False, "Nice")
    assert stored["date"].tzinfo is None, "AppleScript dates are local wall-clock time"
    assert stored["location"] == (48.85, 2.35)
    assert photos.albums["Holiday"].added == ["U1"]


def test_a_disposable_library_takes_an_asset_out_of_an_album_but_keeps_it(tmp_path: Path) -> None:
    photos = DisposableLibrary(tmp_path / "Test.photoslibrary")
    photos.add_existing("U1", "a.jpg")
    photos.add_to_album("To Delete", ["U1"])

    photos.remove_from_album("To Delete", ["U1"])
    photos.remove_from_album("Never made", ["U1"])

    assert photos.albums()["To Delete"] == []
    assert "U1" in photos.assets()


def test_photoscript_takes_assets_out_of_an_album(photos: FakePhotos, tmp_path: Path) -> None:
    library = PhotoscriptLibrary(tmp_path / "Real.photoslibrary")
    library.add_to_album("To Delete", ["U1", "U2"])
    removed: list[str] = []
    photos.albums["To Delete"].remove = lambda found: removed.extend(p.uuid for p in found)  # type: ignore[attr-defined]

    library.remove_from_album("To Delete", ["U1"])

    assert removed == ["U1"]


def test_photoscript_takes_nothing_out_of_an_album_that_does_not_exist(
    photos: FakePhotos, tmp_path: Path
) -> None:
    PhotoscriptLibrary(tmp_path / "Real.photoslibrary").remove_from_album("Never made", ["U1"])

    assert photos.albums == {}


def test_unreadable_photos_settings_say_what_permission_is_missing(
    photos: FakePhotos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "_open_library", lambda: None)

    with pytest.raises(LibraryMismatchError, match="Full Disk Access"):
        PhotoscriptLibrary(tmp_path / "Real.photoslibrary")
