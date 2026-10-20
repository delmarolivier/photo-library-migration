"""Takeout file-naming rules (docs/TAKEOUT_FORMAT.md).

Observed in the real sample: `.supplemental-metadata.json` sidecars, `(N)` moving to before
`.json`, and `-edited` copies. Modelled on public reports and not in the sample: legacy `.json`
sidecars, 47-character media names, 51-character sidecar names, localised edit suffixes.
"""

import pytest

from photomigrate.testing.takeout import (
    EDITED_SUFFIXES,
    duplicate_name,
    edited_name,
    sidecar_name,
    truncate_media_name,
)


def test_supplemental_sidecar_is_media_name_plus_suffix() -> None:
    assert sidecar_name("IMG_1234.JPG") == "IMG_1234.JPG.supplemental-metadata.json"


def test_legacy_sidecar_is_media_name_plus_json() -> None:
    assert sidecar_name("IMG_1234.JPG", style="legacy") == "IMG_1234.JPG.json"


def test_duplicate_index_goes_before_the_media_extension() -> None:
    assert duplicate_name("IMG_1234.JPG", 1) == "IMG_1234(1).JPG"
    assert duplicate_name("IMG_1234.JPG", 0) == "IMG_1234.JPG"


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("supplemental", "IMG_1234.JPG.supplemental-metadata(1).json"),
        ("legacy", "IMG_1234.JPG(1).json"),
    ],
)
def test_duplicate_index_moves_to_just_before_json_in_sidecars(style: str, expected: str) -> None:
    assert sidecar_name("IMG_1234.JPG", style=style, index=1) == expected  # type: ignore[arg-type]


def test_short_media_names_are_kept() -> None:
    assert truncate_media_name("IMG_1234.JPG") == "IMG_1234.JPG"


def test_long_media_names_are_cut_to_47_characters_keeping_the_extension() -> None:
    name = "Screenshot_20191105-123456_A_Rather_Long_Application_Name.jpg"

    truncated = truncate_media_name(name)

    assert len(truncated) == 47
    assert truncated.endswith(".jpg")
    assert name.startswith(truncated.removesuffix(".jpg"))


def test_sidecars_are_cut_to_51_characters_differently_from_the_media() -> None:
    media = truncate_media_name("Screenshot_20191105-123456_A_Rather_Long_Application_Name.jpg")

    legacy = sidecar_name(media, style="legacy")
    supplemental = sidecar_name(media)

    assert legacy == media[:46] + ".json"
    assert legacy.endswith(".jp.json")
    assert len(legacy) == 51
    assert supplemental == legacy


def test_supplemental_suffix_is_cut_first_for_medium_names() -> None:
    assert sidecar_name("PXL_20231105_123456789.MP.jpg") == (
        "PXL_20231105_123456789.MP.jpg.supplemental-met.json"
    )


def test_edited_copy_keeps_the_extension_by_default() -> None:
    assert edited_name("IMG_1234.JPG") == "IMG_1234-edited.JPG"


def test_edited_copy_extension_case_can_differ() -> None:
    assert edited_name("IMG_1234.JPG", extension=".jpg") == "IMG_1234-edited.jpg"


def test_edited_copy_of_a_duplicate_puts_the_index_after_the_suffix() -> None:
    assert edited_name("IMG_1234.JPG", index=1) == "IMG_1234-edited(1).JPG"


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("fr", "IMG_1234-modifié.JPG"),
        ("de", "IMG_1234-bearbeitet.JPG"),
        ("ja", "IMG_1234-編集済み.JPG"),
    ],
)
def test_edited_suffix_is_localised(lang: str, expected: str) -> None:
    assert edited_name("IMG_1234.JPG", lang=lang) == expected


def test_every_localised_suffix_starts_with_a_dash() -> None:
    assert EDITED_SUFFIXES["en"] == "-edited"
    assert all(suffix.startswith("-") for suffix in EDITED_SUFFIXES.values())


def test_edited_names_are_truncated_too_which_can_cut_the_suffix() -> None:
    media = "Screenshot_20191105-123456_LongName_ABCD.jpg"
    assert len(media.removesuffix(".jpg")) == 40

    assert edited_name(media) == "Screenshot_20191105-123456_LongName_ABCD-ed.jpg"
