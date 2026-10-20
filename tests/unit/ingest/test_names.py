"""Media file names: kind, duplicate index, edited copies and dates hidden in names."""

import string
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath

import pytest
from hypothesis import given
from hypothesis import strategies as st

from photomigrate.ingest.names import date_from_filename, media_kind, parse_media_name
from photomigrate.testing.takeout import EDITED_SUFFIXES, duplicate_name, edited_name


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("IMG_1234.JPG", "photo"),
        ("a.heic", "photo"),
        ("IMG_1001-MOTION.gif", "photo"),
        ("scan.tiff", "photo"),
        ("IMG_1010.MOV", "video"),
        ("VID_1.mp4", "video"),
        ("clip.3gp", "video"),
        ("metadata.json", None),
        ("notes.txt", None),
        ("noextension", None),
    ],
)
def test_media_kind_by_extension(name: str, kind: str | None) -> None:
    assert media_kind(name) == kind


def test_plain_name() -> None:
    parsed = parse_media_name("IMG_1234.JPG")

    assert (parsed.base, parsed.index, parsed.stem, parsed.ext) == (
        "IMG_1234.JPG",
        0,
        "IMG_1234",
        ".JPG",
    )
    assert parsed.edited is None


def test_duplicate_index_is_split_off() -> None:
    parsed = parse_media_name("IMG_1234(1).JPG")

    assert (parsed.base, parsed.index) == ("IMG_1234.JPG", 1)


@pytest.mark.parametrize(
    ("name", "original_stem", "index"),
    [
        ("IMG_1234-edited.JPG", "IMG_1234", 0),
        ("IMG_1234-edited.jpg", "IMG_1234", 0),
        ("IMG_1004-edited(1).JPG", "IMG_1004", 1),
        ("IMG_0500-modifié.jpg", "IMG_0500", 0),
        ("IMG_0500-bearbeitet(1).jpg", "IMG_0500", 1),
        ("IMG_1003-SMILE-edited.jpg", "IMG_1003-SMILE", 0),
        ("IMG_1234-編集済み.JPG", "IMG_1234", 0),
    ],
)
def test_edited_copies_name_their_original(name: str, original_stem: str, index: int) -> None:
    parsed = parse_media_name(name)

    assert parsed.edited == original_stem
    assert parsed.index == index


def test_a_cut_edit_suffix_counts_only_at_the_length_limit() -> None:
    at_limit = "Screenshot_20191105-123456_LongName_ABCD-ed.jpg"
    assert len(at_limit) == 47

    assert parse_media_name(at_limit).edited == "Screenshot_20191105-123456_LongName_ABCD"
    assert parse_media_name("photo-ed.jpg").edited is None
    assert parse_media_name("Screenshot_20191105-123456_LongName_ABCDEFG.jpg").edited is None
    assert parse_media_name("IMG_1234-red.jpg").edited is None


def test_decomposed_unicode_is_normalised() -> None:
    decomposed = unicodedata.normalize("NFD", "IMG_0500-modifi\u00e9.jpg")
    assert decomposed != unicodedata.normalize("NFC", decomposed)

    assert parse_media_name(decomposed).edited == "IMG_0500"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("IMG_20120304_101112.jpg", datetime(2012, 3, 4, 10, 11, 12)),
        ("VID_20190704_101010.mp4", datetime(2019, 7, 4, 10, 10, 10)),
        ("PXL_20231105_123456789.jpg", datetime(2023, 11, 5, 12, 34, 56)),
        ("Screenshot_20191105-123456_A_Rather_Long.jpg", datetime(2019, 11, 5, 12, 34, 56)),
        ("2019-07-01 12.34.56.jpg", datetime(2019, 7, 1, 12, 34, 56)),
        ("signal-2021-02-03-040506.jpg", datetime(2021, 2, 3, 4, 5, 6)),
        ("IMG-20190702-WA0003.jpg", datetime(2019, 7, 2)),
        ("PANO_20180101_000001.jpg", datetime(2018, 1, 1, 0, 0, 1)),
        ("IMG_1234.JPG", None),
        ("IMG_20191340_101112.jpg", None),
        ("IMG_18500101_101112.jpg", None),
        ("Screenshot 2019-11-05 at 12.34.56.png", datetime(2019, 11, 5, 12, 34, 56)),
        ("Screen Shot 2019-11-05 at 1.34.56 PM.png", datetime(2019, 11, 5, 13, 34, 56)),
        ("Screen Shot 2019-11-05 at 12.04.56 AM.png", datetime(2019, 11, 5, 0, 4, 56)),
        ("photo_2021-02-03_04-05-06.jpg", datetime(2021, 2, 3, 4, 5, 6)),
        ("Screenshot_2019-11-05-12-34-56-789_com.app.jpg", datetime(2019, 11, 5, 12, 34, 56)),
        ("VID-20190702-WA0003.mp4", datetime(2019, 7, 2)),
        ("IMG_20120304.jpg", datetime(2012, 3, 4)),
        ("2012-03-04.jpg", datetime(2012, 3, 4)),
        ("DSC_123456789.JPG", None),
        ("IMG_20121304.jpg", None),
    ],
)
def test_dates_in_file_names(name: str, expected: datetime | None) -> None:
    assert date_from_filename(name) == expected


_WHEN = st.datetimes(min_value=datetime(1990, 1, 1), max_value=datetime(2099, 12, 31, 23, 59, 59))


def _twelve_hour(when: datetime) -> str:
    hour = when.hour % 12 or 12
    return f"{hour}.{when:%M.%S} {'PM' if when.hour >= 12 else 'AM'}"


_TIMED = [
    lambda d: f"IMG_{d:%Y%m%d_%H%M%S}.jpg",
    lambda d: f"VID_{d:%Y%m%d_%H%M%S}.mp4",
    lambda d: f"PXL_{d:%Y%m%d_%H%M%S}123.jpg",
    lambda d: f"MVIMG_{d:%Y%m%d_%H%M%S}.jpg",
    lambda d: f"Screenshot_{d:%Y%m%d-%H%M%S}_Some App.jpg",
    lambda d: f"{d:%Y%m%d_%H%M%S}.jpg",
    lambda d: f"{d:%Y-%m-%d %H.%M.%S}.jpg",
    lambda d: f"signal-{d:%Y-%m-%d-%H%M%S}.jpg",
    lambda d: f"photo_{d:%Y-%m-%d_%H-%M-%S}.jpg",
    lambda d: f"Screenshot {d:%Y-%m-%d} at {d:%H.%M.%S}.png",
    lambda d: f"Screen Shot {d:%Y-%m-%d} at {_twelve_hour(d)}.png",
    lambda d: f"Screenshot_{d:%Y-%m-%d-%H-%M-%S}-123_com.example.jpg",
]
_DATED = [
    lambda d: f"IMG-{d:%Y%m%d}-WA0042.jpg",
    lambda d: f"VID-{d:%Y%m%d}-WA0001.mp4",
    lambda d: f"IMG_{d:%Y%m%d}.jpg",
    lambda d: f"{d:%Y-%m-%d}.jpg",
]


@given(when=_WHEN, template=st.sampled_from(_TIMED), copy=st.sampled_from(["", "(1)", "-edited"]))
def test_a_capture_time_in_a_name_reads_back_to_the_second(
    when: datetime, template: Callable[[datetime], str], copy: str
) -> None:
    full = template(when)
    suffix = PurePosixPath(full).suffix
    name = full[: -len(suffix)] + copy + suffix

    assert date_from_filename(name) == when.replace(microsecond=0)


@given(when=_WHEN, template=st.sampled_from(_DATED))
def test_a_date_in_a_name_reads_back_as_that_midnight(
    when: datetime, template: Callable[[datetime], str]
) -> None:
    assert date_from_filename(template(when)) == datetime(when.year, when.month, when.day)


@given(
    name=st.from_regex(r"[A-Za-z_ -]{0,12}\d{0,7}[A-Za-z_ -]{0,8}\.(jpg|JPG|mov)", fullmatch=True)
)
def test_names_without_a_date_have_none(name: str) -> None:
    assert date_from_filename(name) is None


_STEM = st.text(string.ascii_letters + string.digits + "_- .é", min_size=1, max_size=40).filter(
    lambda s: (
        s == s.strip(" .")
        and parse_media_name(s + ".jpg").edited is None
        and parse_media_name(s + ".jpg").index == 0
    )
)


@given(
    stem=_STEM,
    ext=st.sampled_from([".jpg", ".JPG", ".heic", ".MOV", ".mp4"]),
    index=st.integers(0, 12),
    lang=st.none() | st.sampled_from(sorted(EDITED_SUFFIXES)),
)
def test_parsing_recovers_what_takeout_encoded_in_a_name(
    stem: str, ext: str, index: int, lang: str | None
) -> None:
    base = stem + ext
    name = edited_name(base, lang=lang, index=index) if lang else duplicate_name(base, index)

    parsed = parse_media_name(name)

    assert parsed.index == index
    assert parsed.ext == ext
    assert parsed.edited == (stem if lang else None)
    if not lang:
        assert parsed.base == base


def test_a_cut_suffix_is_found_even_when_an_index_follows() -> None:
    name = "0" * 33 + "-bearbeite(1).jpg"

    parsed = parse_media_name(name)

    assert parsed.edited == "0" * 33
    assert parsed.index == 1
