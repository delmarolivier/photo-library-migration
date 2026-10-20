"""Sidecar and album metadata JSON, shaped like the real sample (docs/TAKEOUT_FORMAT.md)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from photomigrate.testing.takeout import Geo, album_metadata, format_timestamp, sidecar_payload

TAKEN = datetime(2019, 7, 1, 12, 34, 56, tzinfo=timezone(timedelta(hours=2)))
UPLOADED = datetime(2019, 9, 3, 8, 0, 5, tzinfo=UTC)


def payload(**overrides: object) -> dict[str, object]:
    args: dict[str, object] = {
        "title": "IMG_1234.JPG",
        "taken": TAKEN,
        "created": UPLOADED,
        "url": "https://photos.google.com/photo/AF1QipTEST",
    }
    args.update(overrides)
    return sidecar_payload(**args)  # type: ignore[arg-type]


def test_timestamps_are_epoch_seconds_as_strings_with_a_formatted_twin() -> None:
    data = payload()

    assert data["photoTakenTime"] == {
        "timestamp": "1561977296",
        "formatted": "1 Jul 2019, 10:34:56 UTC",
    }
    assert data["creationTime"] == {
        "timestamp": "1567497605",
        "formatted": "3 Sept 2019, 08:00:05 UTC",
    }


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en_GB", "20 Sept 2024, 22:11:12 UTC"),
        ("en_US", "Sep 20, 2024, 10:11:12 PM UTC"),
    ],
)
def test_formatted_dates_depend_on_locale(locale: str, expected: str) -> None:
    when = datetime(2024, 9, 20, 22, 11, 12, tzinfo=UTC)

    assert format_timestamp(when, locale) == expected  # type: ignore[arg-type]


def test_us_format_handles_midnight_and_noon() -> None:
    assert format_timestamp(datetime(2024, 1, 2, 0, 5, tzinfo=UTC), "en_US") == (
        "Jan 2, 2024, 12:05:00 AM UTC"
    )
    assert format_timestamp(datetime(2024, 1, 2, 12, 5, tzinfo=UTC), "en_US") == (
        "Jan 2, 2024, 12:05:00 PM UTC"
    )


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        payload(taken=datetime(2019, 7, 1, 12, 0))


def test_unknown_location_is_all_zeros_and_exif_location_is_optional() -> None:
    data = payload()

    assert data["geoData"] == {
        "latitude": 0.0,
        "longitude": 0.0,
        "altitude": 0.0,
        "latitudeSpan": 0.0,
        "longitudeSpan": 0.0,
    }
    assert "geoDataExif" not in data


def test_known_location_fills_geo_data_and_optionally_geo_data_exif() -> None:
    nice = Geo(43.6970333, 7.266, altitude=12.5)

    data = payload(geo=nice, geo_exif=nice)

    assert data["geoData"] == {
        "latitude": 43.6970333,
        "longitude": 7.266,
        "altitude": 12.5,
        "latitudeSpan": 0.0,
        "longitudeSpan": 0.0,
    }
    assert data["geoDataExif"] == data["geoData"]


def test_field_order_and_defaults_match_the_real_sample() -> None:
    data = payload()

    assert list(data) == [
        "title",
        "description",
        "imageViews",
        "creationTime",
        "photoTakenTime",
        "geoData",
        "url",
        "googlePhotosOrigin",
    ]
    assert data["description"] == ""
    assert data["imageViews"] == "0"
    assert data["googlePhotosOrigin"] == {"photosDesktopUploader": {}}


def test_people_favourites_and_descriptions_are_written_when_present() -> None:
    data = payload(
        description="Plage à Nice 🏖️", favorited=True, people=("Alice Example", "Bob Example")
    )

    assert data["description"] == "Plage à Nice 🏖️"
    assert data["favorited"] is True
    assert data["people"] == [{"name": "Alice Example"}, {"name": "Bob Example"}]


def test_favorited_is_absent_unless_true() -> None:
    assert "favorited" not in payload(favorited=False)


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("desktop", {"photosDesktopUploader": {}}),
        ("mobile", {"mobileUpload": {"deviceType": "IOS_PHONE"}}),
        ("auto", {"composition": {"type": "AUTO"}}),
    ],
)
def test_upload_origin(origin: str, expected: dict[str, object]) -> None:
    assert payload(origin=origin)["googlePhotosOrigin"] == expected


def test_album_metadata_has_title_and_optional_description_and_comments() -> None:
    assert album_metadata("Été 2019") == {"title": "Été 2019"}

    data = album_metadata(
        "Été 2019",
        description="Vacances 🌞",
        comments=[("Alice Example", UPLOADED)],
    )

    assert data == {
        "title": "Été 2019",
        "description": "Vacances 🌞",
        "sharedAlbumComments": [
            {
                "contentOwnerName": "Alice Example",
                "creationTime": {
                    "timestamp": "1567497605",
                    "formatted": "3 Sept 2019, 08:00:05 UTC",
                },
                "liked": True,
            }
        ],
    }
