"""Faces in a photo, for best-shot ranking (CLA-84): Apple Vision on macOS, nothing elsewhere.

Ingest and index read the faces once, from the picture already decoded for hashing (upright,
long side at most 1024 px), and store them as JSON (``items.faces``).

* Vision (``VNDetectFaceCaptureQualityRequest``) finds the faces and gives each its capture
  quality, from 0 to 1: sharp, well lit, facing the camera.
* Vision has no eye-state classifier, so Core Image's face detector (``CIDetector`` with
  ``CIDetectorEyeBlink``) says whether the eyes are closed. Its faces are matched to Vision's
  by position. Both detectors give boxes as fractions of the picture, origin bottom-left.
  ``eyes_open`` is ``None`` for a face Core Image missed.

``face_reader()`` is ``None`` where Vision is unavailable (Linux, or no pyobjc). Items then
keep ``faces = NULL`` (not measured, unlike ``[]``: measured, no faces), and best-shot ranks
their groups without the face scores.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image

type Box = tuple[float, float, float, float]
"""x, y, width, height, as fractions of the picture; origin bottom-left."""


@dataclass(frozen=True)
class Face:
    box: Box
    quality: float
    eyes_open: bool | None


class FaceReader(Protocol):
    def read(self, image: Image.Image) -> list[Face]: ...


def _centre(box: Box) -> tuple[float, float]:
    x, y, width, height = box
    return x + width / 2, y + height / 2


def _inside(point: tuple[float, float], box: Box) -> bool:
    x, y, width, height = box
    return x <= point[0] <= x + width and y <= point[1] <= y + height


def match_eyes(
    vision: Sequence[tuple[Box, float]], blinks: Sequence[tuple[Box, bool, bool]]
) -> list[Face]:
    """Vision's faces (box, quality), each with the eye state of the Core Image face (box, left
    eye closed, right eye closed) whose centre lies inside it, the closest centre first. Each
    Core Image face is used once. One closed eye is a blink."""
    unused = list(blinks)
    found = []
    for box, quality in vision:
        cx, cy = _centre(box)
        inside = [b for b in unused if _inside(_centre(b[0]), box)]
        eyes_open = None
        if inside:
            nearest = min(
                inside, key=lambda b: (_centre(b[0])[0] - cx) ** 2 + (_centre(b[0])[1] - cy) ** 2
            )
            unused.remove(nearest)
            eyes_open = not (nearest[1] or nearest[2])
        found.append(Face(box, quality, eyes_open))
    return found


def faces_to_json(faces: Sequence[Face] | None) -> str | None:
    if faces is None:
        return None
    return json.dumps(
        [{"box": list(f.box), "quality": f.quality, "eyes_open": f.eyes_open} for f in faces]
    )


def faces_from_json(text: str | None) -> list[Face] | None:
    if text is None:
        return None
    return [
        Face(tuple(face["box"]), face["quality"], face["eyes_open"]) for face in json.loads(text)
    ]


def _box(rect: Any, width: float = 1.0, height: float = 1.0) -> Box:
    return (
        round(rect.origin.x / width, 4),
        round(rect.origin.y / height, 4),
        round(rect.size.width / width, 4),
        round(rect.size.height / height, 4),
    )


class VisionFaces:
    """Faces by Apple Vision, eye states by Core Image (macOS only)."""

    def __init__(self) -> None:
        import Quartz

        self._detector = Quartz.CIDetector.detectorOfType_context_options_(
            Quartz.CIDetectorTypeFace,
            None,
            {Quartz.CIDetectorAccuracy: Quartz.CIDetectorAccuracyHigh},
        )

    def read(self, image: Image.Image) -> list[Face]:
        import Quartz
        import Vision
        from Foundation import NSData

        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "TIFF")  # uncompressed: quick to write and to read
        raw = buffer.getvalue()
        data = NSData.dataWithBytes_length_(raw, len(raw))

        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
        request = Vision.VNDetectFaceCaptureQualityRequest.alloc().init()
        done, error = handler.performRequests_error_([request], None)
        if not done:
            raise RuntimeError(f"Vision could not read the picture: {error}")
        vision = [
            (_box(face.boundingBox()), round(float(face.faceCaptureQuality()), 4))
            for face in request.results() or ()
        ]

        picture = Quartz.CIImage.imageWithData_(data)
        size = picture.extent().size
        blinks = [
            (
                _box(face.bounds(), size.width, size.height),
                face.leftEyeClosed(),
                face.rightEyeClosed(),
            )
            for face in self._detector.featuresInImage_options_(
                picture, {Quartz.CIDetectorEyeBlink: True}
            )
        ]
        return match_eyes(vision, blinks)


def face_reader() -> FaceReader | None:
    """Vision on macOS, or ``None`` where it is unavailable."""
    if sys.platform != "darwin":
        return None
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
    except ImportError:
        return None
    return VisionFaces()
