"""Faces for best-shot ranking: Vision's faces with Core Image's eye state, or nothing."""

import builtins
import sys
from typing import Any

import pytest

from photomigrate import faces
from photomigrate.faces import Face, faces_from_json, faces_to_json, match_eyes

BOX = (0.29, 0.615, 0.067, 0.1)


def test_each_vision_face_takes_the_eye_state_of_the_core_image_face_inside_it() -> None:
    found = match_eyes(
        [(BOX, 0.341), ((0.5, 0.5, 0.1, 0.1), 0.7)],
        [((0.689, 0.529, 0.047, 0.082), False, False), ((0.28, 0.607, 0.063, 0.098), True, True)],
    )

    assert found == [Face(BOX, 0.341, eyes_open=False), Face((0.5, 0.5, 0.1, 0.1), 0.7, None)]


def test_one_closed_eye_is_a_blink_and_each_core_image_face_is_used_once() -> None:
    found = match_eyes(
        [(BOX, 0.5), (BOX, 0.4)],
        [((0.28, 0.607, 0.063, 0.098), False, True)],
    )

    assert [face.eyes_open for face in found] == [False, None]


def test_the_closest_centre_wins_when_two_core_image_faces_fall_inside() -> None:
    wide = (0.0, 0.0, 1.0, 1.0)
    found = match_eyes(
        [(wide, 0.5)],
        [((0.0, 0.0, 0.1, 0.1), True, True), ((0.45, 0.45, 0.1, 0.1), False, False)],
    )

    assert found[0].eyes_open is True


def test_faces_round_trip_through_json_and_none_means_not_measured() -> None:
    measured = [Face(BOX, 0.341, eyes_open=False), Face((0.5, 0.5, 0.1, 0.1), 0.7, None)]

    assert faces_from_json(faces_to_json(measured)) == measured
    assert faces_from_json(faces_to_json([])) == []
    assert faces_to_json(None) is None
    assert faces_from_json(None) is None


def test_there_is_no_face_reader_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    assert faces.face_reader() is None


def test_there_is_no_face_reader_without_pyobjc_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def without_vision(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in ("Vision", "Quartz"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(builtins, "__import__", without_vision)

    assert faces.face_reader() is None
