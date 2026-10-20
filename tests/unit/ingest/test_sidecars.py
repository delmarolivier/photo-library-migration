"""Sidecar parsing and media-to-sidecar matching (every M1-02 quirk, plus property tests)."""

import json
import string
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import PurePosixPath

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from photomigrate.ingest.sidecars import (
    SidecarData,
    SidecarError,
    is_album_metadata,
    match_folder,
    parse_sidecar,
)

from photomigrate.ingest.names import parse_media_name
from photomigrate.testing.takeout import (
    EDITED_SUFFIXES,
    Geo,
    TakeoutManifest,
    duplicate_name,
    edited_name,
    sidecar_name,
    sidecar_payload,
    truncate_media_name,
)

TAKEN = datetime(2019, 7, 1, 12, 34, 56, tzinfo=timezone(timedelta(hours=2)))
UPLOADED = datetime(2019, 7, 3, 15, 0, tzinfo=UTC)


def encoded(**overrides: object) -> bytes:
    args: dict[str, object] = {
        "title": "IMG_1001.JPG",
        "taken": TAKEN,
        "created": UPLOADED,
        "url": "https://photos.google.com/photo/AF1QipX",
    }
    args.update(overrides)
    return json.dumps(sidecar_payload(**args)).encode()  # type: ignore[arg-type]


# --- parsing -------------------------------------------------------------------------------------


def test_parse_a_full_sidecar() -> None:
    data = parse_sidecar(
        encoded(
            geo=Geo(43.6970333, 7.266, 12.0),
            geo_exif=Geo(43.69, 7.26),
            description="Plage à Nice 🏖️",
            favorited=True,
            people=("Alice Example", "Bob Example"),
            origin="mobile",
        )
    )

    assert data.title == "IMG_1001.JPG"
    assert data.description == "Plage à Nice 🏖️"
    assert data.taken == TAKEN
    assert data.taken is not None
    assert data.taken.tzinfo == UTC
    assert data.created == UPLOADED
    assert data.geo == (43.6970333, 7.266, 12.0)
    assert data.geo_exif == (43.69, 7.26, 0.0)
    assert data.favorited is True
    assert data.people == ("Alice Example", "Bob Example")
    assert data.origin == "mobile"


def test_unknown_location_and_missing_fields_become_none() -> None:
    raw = json.loads(encoded())
    del raw["photoTakenTime"]

    data = parse_sidecar(json.dumps(raw).encode())

    assert data.taken is None
    assert data.geo is None
    assert data.geo_exif is None
    assert data.favorited is False
    assert data.people == ()
    assert data.origin == "desktop"


def test_numeric_timestamps_and_unknown_origins_are_accepted() -> None:
    raw = json.loads(encoded())
    raw["photoTakenTime"]["timestamp"] = 0
    raw["googlePhotosOrigin"] = {"somethingNew": {}}

    data = parse_sidecar(json.dumps(raw).encode())

    assert data.taken == datetime(1970, 1, 1, tzinfo=UTC)
    assert data.origin is None


def test_auto_creations_are_recognised() -> None:
    assert parse_sidecar(encoded(origin="auto")).origin == "auto"


@pytest.mark.parametrize(
    "payload", [b"{not json", b"[]", b'{"photoTakenTime": {"timestamp": "x"}}']
)
def test_broken_sidecars_raise(payload: bytes) -> None:
    with pytest.raises(SidecarError):
        parse_sidecar(payload)


def test_sidecar_data_round_trips_through_plain_dicts() -> None:
    data = parse_sidecar(encoded(geo=Geo(1.5, 2.5), people=("Alice Example",)))

    assert SidecarData.from_dict(json.loads(json.dumps(data.to_dict()))) == data


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("metadata.json", True),
        ("métadonnées.json", True),
        ("Metadaten.json", True),
        ("print-subscriptions.json", True),
        ("IMG_1001.JPG.json", False),
    ],
)
def test_album_metadata_and_other_known_json(name: str, expected: bool) -> None:
    assert is_album_metadata(name) is expected


# --- matching examples ---------------------------------------------------------------------------


def sidecar_for(media: list[str], jsons: list[str], name: str) -> str | None:
    return match_folder(media, jsons).matches[name].sidecar


def test_exact_supplemental_and_legacy() -> None:
    folder = match_folder(
        ["IMG_1001.JPG", "IMG_0500.jpg"],
        ["IMG_1001.JPG.supplemental-metadata.json", "IMG_0500.jpg.json"],
    )

    assert folder.matches["IMG_1001.JPG"].sidecar == "IMG_1001.JPG.supplemental-metadata.json"
    assert folder.matches["IMG_1001.JPG"].method == "exact"
    assert folder.matches["IMG_0500.jpg"].sidecar == "IMG_0500.jpg.json"
    assert folder.orphans == ()


def test_duplicate_index_in_both_styles() -> None:
    media = ["IMG_1004.JPG", "IMG_1004(1).JPG", "IMG_0500.jpg", "IMG_0500(1).jpg"]
    jsons = [
        "IMG_1004.JPG.supplemental-metadata.json",
        "IMG_1004.JPG.supplemental-metadata(1).json",
        "IMG_0500.jpg.json",
        "IMG_0500.jpg(1).json",
    ]

    folder = match_folder(media, jsons)

    assert folder.matches["IMG_1004(1).JPG"].sidecar == "IMG_1004.JPG.supplemental-metadata(1).json"
    assert folder.matches["IMG_0500(1).jpg"].sidecar == "IMG_0500.jpg(1).json"
    assert folder.matches["IMG_1004.JPG"].sidecar == "IMG_1004.JPG.supplemental-metadata.json"


def test_a_literal_index_in_an_uploaded_name_still_matches() -> None:
    assert sidecar_for(
        ["Party(2).jpg"], ["Party(2).jpg.supplemental-metadata.json"], "Party(2).jpg"
    ) == ("Party(2).jpg.supplemental-metadata.json")


@pytest.mark.parametrize("style", ["supplemental", "legacy"])
def test_truncated_names(style: str) -> None:
    media = truncate_media_name("Screenshot_20191105-123456_A_Rather_Long_Application_Name.jpg")
    sidecar = sidecar_name(media, style=style)  # type: ignore[arg-type]

    folder = match_folder([media], [sidecar])

    assert folder.matches[media].sidecar == sidecar
    assert folder.matches[media].method == "truncated"


def test_partially_cut_supplemental_suffix() -> None:
    assert (
        sidecar_for(
            ["PXL_20231105_123456789.MP.jpg"],
            ["PXL_20231105_123456789.MP.jpg.supplemental-met.json"],
            "PXL_20231105_123456789.MP.jpg",
        )
        == "PXL_20231105_123456789.MP.jpg.supplemental-met.json"
    )


def test_legacy_sidecar_without_media_extension() -> None:
    folder = match_folder(["IMG_1234.jpg"], ["IMG_1234.json"])

    assert folder.matches["IMG_1234.jpg"].sidecar == "IMG_1234.json"
    assert folder.matches["IMG_1234.jpg"].method == "no-extension"


@pytest.mark.parametrize(
    ("original", "edit"),
    [
        ("IMG_1001.JPG", "IMG_1001-edited.JPG"),
        ("IMG_1005.JPG", "IMG_1005-edited.jpg"),
        ("IMG_1004(1).JPG", "IMG_1004-edited(1).JPG"),
        ("IMG_0500.jpg", "IMG_0500-modifié.jpg"),
        ("IMG_1003-SMILE.jpg", "IMG_1003-SMILE-edited.jpg"),
        (
            "Screenshot_20191105-123456_LongName_ABCD.jpg",
            "Screenshot_20191105-123456_LongName_ABCD-ed.jpg",
        ),
    ],
)
def test_edited_copies_use_the_original_sidecar(original: str, edit: str) -> None:
    parsed = parse_media_name(original)
    sidecar = sidecar_name(parsed.base, index=parsed.index)

    folder = match_folder([original, edit], [sidecar])

    assert folder.matches[edit].sidecar == sidecar
    assert folder.matches[edit].method == "edited-original"
    assert folder.matches[edit].original == original
    assert folder.matches[original].sidecar == sidecar


def test_edited_copy_of_an_original_without_sidecar_is_unmatched() -> None:
    folder = match_folder(["IMG_1006.JPG", "IMG_1006-edited.JPG"], [])

    assert folder.matches["IMG_1006-edited.JPG"].method == "unmatched"
    assert folder.matches["IMG_1006-edited.JPG"].original == "IMG_1006.JPG"


def test_edited_copy_whose_original_is_missing_finds_the_sidecar_by_name() -> None:
    folder = match_folder(["IMG_2001-edited.jpg"], ["IMG_2001.HEIC.supplemental-metadata.json"])

    match = folder.matches["IMG_2001-edited.jpg"]
    assert match.sidecar == "IMG_2001.HEIC.supplemental-metadata.json"
    assert match.method == "edited-original"
    assert match.original is None


def test_unmatched_media_and_orphan_sidecars_are_reported() -> None:
    folder = match_folder(
        ["IMG_1006.JPG", "IMG_1011.MOV"],
        ["IMG_9999.JPG.supplemental-metadata.json", "metadata.json"],
    )

    assert {m.method for m in folder.matches.values()} == {"unmatched"}
    assert folder.orphans == ("IMG_9999.JPG.supplemental-metadata.json",)


def test_higher_scoring_media_claim_their_sidecar_first() -> None:
    # "a.jpg.jpeg" could read "a.jpg.json" as its extension-less sidecar, but it is a.jpg's.
    folder = match_folder(["a.jpg.jpeg", "a.jpg"], ["a.jpg.json", "a.jpg.jpeg.json"])

    assert folder.matches["a.jpg"].sidecar == "a.jpg.json"
    assert folder.matches["a.jpg.jpeg"].sidecar == "a.jpg.jpeg.json"


def test_a_truncated_name_that_looks_edited_keeps_its_own_sidecar() -> None:
    media = truncate_media_name("Summer_2019_at_the_beach_with_the_family-moon_party.jpg")
    assert parse_media_name(media).edited is not None

    assert sidecar_for([media], [sidecar_name(media)], media) == sidecar_name(media)


def test_names_are_compared_after_unicode_normalisation() -> None:
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Été.jpg")

    assert (
        sidecar_for([nfd], ["Été.jpg.supplemental-metadata.json"], nfd)
        == "Été.jpg.supplemental-metadata.json"
    )


# --- the standard Takeout ------------------------------------------------------------------------


def test_every_standard_takeout_item_gets_its_true_sidecar(
    standard_takeout_zips: TakeoutManifest,
) -> None:
    m = standard_takeout_zips
    by_folder: dict[str, tuple[list[str], list[str]]] = defaultdict(lambda: ([], []))
    for path in m.files:
        parts = PurePosixPath(path).parts
        if len(parts) == 4 and parts[1] == "Google Photos":
            media, jsons = by_folder[parts[2]]
            (jsons if path.endswith(".json") else media).append(parts[3])

    for folder, (media, jsons) in by_folder.items():
        result = match_folder(media, jsons)
        assert result.orphans == (), folder
        for item in (i for i in m.items if i.folder == folder):
            expected = m.metadata_sidecar(item)
            got = result.matches[item.name].sidecar
            assert got == (PurePosixPath(expected).name if expected else None), item.path


# --- property tests ------------------------------------------------------------------------------

STEM_CHARS = string.ascii_letters + string.digits + "_- .éü日"
EXTENSIONS = [".jpg", ".JPG", ".jpeg", ".heic", ".HEIC", ".png", ".gif", ".mp4", ".MOV"]


def plain_stem(stem: str) -> bool:
    if stem != stem.strip(" .") or not stem:
        return False
    parsed = parse_media_name(stem + ".jpg")
    return parsed.edited is None and parsed.index == 0


specs = st.lists(
    st.tuples(
        st.text(STEM_CHARS, min_size=1, max_size=60).filter(plain_stem),
        st.sampled_from(EXTENSIONS),
        st.sampled_from(["supplemental", "legacy"]),
        st.booleans(),
        st.none() | st.tuples(st.sampled_from(sorted(EDITED_SUFFIXES)), st.booleans()),
    ),
    min_size=1,
    max_size=6,
)


@given(specs)
def test_matching_recovers_the_sidecar_google_would_write(
    folder_spec: list[tuple[str, str, str, bool, tuple[str, bool] | None]],
) -> None:
    seen: dict[str, int] = {}
    media: list[str] = []
    jsons: list[str] = []
    expected: dict[str, str | None] = {}
    for stem, ext, style, has_sidecar, edit in folder_spec:
        base = truncate_media_name(stem + ext)
        index = seen.get(base, 0)
        seen[base] = index + 1
        name = duplicate_name(base, index)
        sidecar = sidecar_name(base, style=style, index=index) if has_sidecar else None  # type: ignore[arg-type]
        media.append(name)
        jsons += [sidecar] if sidecar else []
        expected[name] = sidecar
        if edit:
            lang, flip = edit
            edited = edited_name(
                base, lang=lang, extension=ext.swapcase() if flip else None, index=index
            )
            assume(parse_media_name(edited).edited is not None)
            media.append(edited)
            expected[edited] = sidecar
    assume(len(set(media)) == len(media))
    assume(len(set(jsons)) == len(jsons))

    result = match_folder(media, jsons)

    assert set(result.matches) == set(media)
    assert {name: m.sidecar for name, m in result.matches.items()} == expected
    assert result.orphans == ()
