"""Thumbnails: posters for videos, placeholders when the file is not at hand."""

import io
from pathlib import Path

import pytest
from PIL import Image

from photomigrate.catalogue import CatalogueItem
from photomigrate.review import thumbs
from photomigrate.review.thumbs import Thumbnails, placeholder, poster
from photomigrate.testing.media import encode_video, video_frames


def clip(tmp_path: Path, seconds: float) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(encode_video(video_frames(1, seconds), "mp4"))
    return path


def test_a_poster_is_a_frame_of_the_video(tmp_path: Path) -> None:
    frame = poster(clip(tmp_path, 1.0))

    assert frame is not None
    assert frame.size == (64, 48)


def test_a_clip_shorter_than_half_a_second_shows_its_first_frame(tmp_path: Path) -> None:
    assert poster(clip(tmp_path, 0.2)) is not None


def test_no_poster_without_ffmpeg_or_from_a_broken_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")

    video = clip(tmp_path, 1.0)

    assert poster(broken) is None
    monkeypatch.setattr(thumbs.shutil, "which", lambda name: None)
    assert poster(video) is None


def placeholder_for(item: CatalogueItem, tmp_path: Path, **sources: Path) -> bytes:
    store = Thumbnails(tmp_path / "cache", **sources)  # type: ignore[arg-type]
    try:
        return store.get(item)
    finally:
        store.close()


@pytest.mark.parametrize(
    "item",
    [
        CatalogueItem(source="google", source_key="a.jpg", kind="photo"),
        CatalogueItem(source="google", source_key="a.jpg", kind="photo", original_path="x::a.jpg"),
        CatalogueItem(source="icloud", source_key="U", kind="photo", original_path="o/a.jpg"),
        CatalogueItem(source="icloud", source_key="U", kind="video", original_path="o/a.mov"),
    ],
    ids=["no original", "no takeout", "no library", "missing file"],
)
def test_items_whose_file_is_not_at_hand_get_a_placeholder(
    item: CatalogueItem, tmp_path: Path
) -> None:
    library = tmp_path / "Library.photoslibrary"
    library.mkdir()
    sources = {"library": library} if item.kind == "video" else {}

    assert placeholder_for(item, tmp_path, **sources) == placeholder()
    assert not (tmp_path / "cache").exists()


def test_an_entry_missing_from_the_takeout_gets_a_placeholder(tmp_path: Path) -> None:
    takeout = tmp_path / "Takeout" / "Google Photos" / "Photos from 2020"
    takeout.mkdir(parents=True)
    Image.new("RGB", (40, 30)).save(takeout / "real.jpg")
    item = CatalogueItem(source="google", source_key="a.jpg", kind="photo", original_path="nope")

    store = Thumbnails(tmp_path / "cache", takeout=tmp_path)
    try:
        assert store.get(item) == placeholder()
        real = item.replace(original_path="Takeout/Google Photos/Photos from 2020/real.jpg")
        assert Image.open(io.BytesIO(store.get(real))).size == (40, 30)
    finally:
        store.close()
