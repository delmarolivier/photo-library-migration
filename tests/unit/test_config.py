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


def test_best_shot_defaults_are_a_two_minute_window_and_weights_for_every_score() -> None:
    best_shot = Config().best_shot

    assert best_shot.window_seconds == 120.0
    assert 0 < best_shot.min_similarity < 1
    weights = best_shot.weights
    assert {name: getattr(weights, name) > 0 for name in weights.names()} == {
        "sharpness": True,
        "exposure": True,
        "faces": True,
        "eyes_open": True,
        "aesthetic": True,
    }


def test_best_shot_window_and_weights_can_be_overridden(tmp_path: Path) -> None:
    config = load_config(
        write(tmp_path, "[best_shot]\nwindow_seconds = 30\n[best_shot.weights]\nfaces = 0\n")
    )

    assert config.best_shot.window_seconds == 30.0
    assert config.best_shot.weights.faces == 0.0
    assert config.best_shot.weights.sharpness == Config().best_shot.weights.sharpness


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[best_shot]\nmin_similarity = 1.5\n", "'best_shot.min_similarity' must be at most 1"),
        (
            "[best_shot.weights]\nsharpness = 0\nexposure = 0\nfaces = 0\neyes_open = 0\n"
            "aesthetic = 0\n",
            "at least one 'best_shot.weights' setting must be above 0",
        ),
    ],
)
def test_best_shot_settings_that_cannot_work_are_errors(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(write(tmp_path, text))


def test_the_clutter_threshold_can_be_raised_but_not_past_one(tmp_path: Path) -> None:
    assert Config().clutter.min_score == 0.5
    assert load_config(write(tmp_path, "[clutter]\nmin_score = 0.7\n")).clutter.min_score == 0.7
    with pytest.raises(ConfigError, match=re.escape("'clutter.min_score' must be at most 1")):
        load_config(write(tmp_path, "[clutter]\nmin_score = 1.5\n"))


def test_the_location_window_and_timeline_tolerance_can_be_set(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "[location]\nwindow_minutes = 10\n"))

    assert config.location.window_minutes == 10.0
    assert config.location.timeline_tolerance_minutes == 15.0


def test_switches_are_true_or_false(tmp_path: Path) -> None:
    assert Config().location.any_device is False
    assert load_config(write(tmp_path, "[location]\nany_device = true\n")).location.any_device
    with pytest.raises(ConfigError, match=re.escape("'location.any_device' must be true or false")):
        load_config(write(tmp_path, "[location]\nany_device = 1\n"))
