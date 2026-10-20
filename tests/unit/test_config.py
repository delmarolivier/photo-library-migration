"""The TOML config file: built-in defaults, overrides, and clear errors for mistakes."""

import re
from pathlib import Path

import pytest

from photomigrate.config import Config, ConfigError, load_config

ROOT = Path(__file__).resolve().parents[2]


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "photomigrate.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_no_file_means_the_built_in_defaults() -> None:
    assert load_config(None) == Config()


def test_the_example_file_documents_exactly_the_defaults() -> None:
    assert load_config(ROOT / "photomigrate.example.toml") == Config()


def test_one_threshold_can_be_overridden(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "[quality.photo]\nblurry_below_sharpness = 12.5\n"))

    assert config.quality.photo.blurry_below_sharpness == 12.5
    assert config.quality.photo.dark_above_fraction == Config().quality.photo.dark_above_fraction
    assert config.quality.video == Config().quality.video


def test_whole_numbers_are_fine_for_decimal_thresholds(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "[quality.video]\nshort_below_seconds = 2\n"))

    assert config.quality.video.short_below_seconds == 2.0
    assert isinstance(config.quality.video.short_below_seconds, float)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[qualty.photo]\nblurry_below_sharpness = 1\n", "unknown section 'qualty'"),
        ("[quality.foto]\n", "unknown section 'quality.foto'"),
        ("[quality.photo]\nblury_below_sharpness = 1\n", "unknown setting 'quality.photo.blury"),
        ("[quality.photo]\nblurry_below_sharpness = 'low'\n", "must be a number"),
        ("[quality.photo]\nblurry_below_sharpness = true\n", "must be a number"),
        ("[quality.photo]\nblurry_below_sharpness = -1\n", "must not be negative"),
        ("[quality.video]\ntiny_below_short_side = 240.5\n", "must be a whole number"),
        ("[quality]\nphoto = 3\n", "'quality.photo' must be a table"),
        ("[quality\n", "not valid TOML"),
    ],
)
def test_mistakes_are_reported_with_the_setting_name(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(write(tmp_path, text))


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no config file"):
        load_config(tmp_path / "nope.toml")
