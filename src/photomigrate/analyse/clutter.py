"""Clutter: a bulk-review queue of screenshots, documents, memes and forwards (CLA-86).

Every item gets a score from 0 to 1 per category; the best category, if it reaches
``min_score`` (config ``[clutter]``), puts the item in the queue (``clutter`` table) with its
evidence. Nothing is decided: the review gallery shows the queue.

Each category's score combines its clues as independent pieces of evidence
(``1 - Π(1 - weight)``), and a camera's make and model in the file count against the
categories a camera does not make:

* ``google-creation``: Google's own animations and composites (``google-creation`` from
  ingest: sidecar origin AUTO, 1.0), or names ending ``-MOTION``, ``-MIX``, ``-SMILE``,
  ``-ANIMATION``, ``-COLLAGE`` or ``-EFFECTS`` (0.9). Never imported automatically (Delmar).
* ``screenshot``: Photos says so (0.95), a screenshot name in several languages (0.8), a
  photo the size of a device screen (0.45: never enough alone), PNG (0.3), no camera and no
  file date (0.2). A camera: times 0.25.
* ``document`` (receipts, scans, letters): Vision's text regions (0.7 at 12 or more), with 3 or
  more regions the text's coverage (0.5 at 20%) and white paper (0.3), or without Vision, the
  open_clip
  zero-shot label (0.9 times its probability). No camera adds 0.15, but only to other evidence,
  so a missing camera never makes a document, and a street sign in a camera photo stays a
  photo.
* ``meme``: big captions at the top or bottom (0.6), some text over a picture (0.2), a saved
  or downloaded name (0.3), no camera and no file date (0.2), or the zero-shot label
  (0.8 times its probability). A camera: times 0.2; a page of text: times 0.5.
* ``forward`` (messaging): WhatsApp's ``-WA0001`` names (0.85), other messengers' names
  (0.6), stripped metadata (0.2), a messaging size (0.25). A camera: times 0.3.

Zero-shot labels come from the photo's stored embedding and open_clip's text prompts, only
when Vision measured no text (Linux). Google edits share their original's answer and are not
queued themselves, unless their original is missing (``unpaired-edit``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, cast

import numpy as np

from photomigrate.catalogue import Catalogue, CatalogueItem, Kind, Source
from photomigrate.config import ClutterConfig
from photomigrate.embedding import Embedder, Vector, from_bytes
from photomigrate.ingest.names import parse_media_name
from photomigrate.runner import Action, JSONValue
from photomigrate.text_regions import TextSummary, text_from_json

Category = Literal["google-creation", "screenshot", "document", "meme", "forward"]
CATEGORIES: tuple[Category, ...] = ("google-creation", "screenshot", "document", "meme", "forward")
"""In the order that breaks ties."""

_SCREENSHOT_NAME = re.compile(
    r"screen ?shot|capture d.écran|bildschirmfoto|bildschirmaufnahme|schermafbeelding|"
    r"captura de pantalla|schermata|スクリーンショット|screen ?recording",
    re.IGNORECASE,
)
_CREATION_SUFFIX = re.compile(r"-(MOTION|MIX|SMILE|ANIMATION|COLLAGE|EFFECTS)$", re.IGNORECASE)
_WHATSAPP = re.compile(r"-WA\d+", re.IGNORECASE)
_MESSAGING = re.compile(
    r"^(received_|FB_IMG_|Messenger_|signal-|telegram|photo_\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)",
    re.IGNORECASE,
)
_SAVED = re.compile(
    r"^(received_|FB_IMG_|download|image(\s*\(\d+\))?\.|images?_|9gag|reddit|imgur|pinterest)",
    re.IGNORECASE,
)
SCREEN_SIZES = frozenset(
    {
        # iPhone
        (640, 960), (640, 1136), (750, 1334), (828, 1792), (1080, 1920), (1125, 2436),
        (1170, 2532), (1179, 2556), (1206, 2622), (1242, 2208), (1242, 2688), (1284, 2778),
        (1290, 2796), (1320, 2868),
        # Android
        (720, 1280), (720, 1600), (1080, 2160), (1080, 2220), (1080, 2280), (1080, 2340),
        (1080, 2400), (1440, 2560), (1440, 2960), (1440, 3040), (1440, 3200),
        # iPad
        (1536, 2048), (1620, 2160), (1640, 2360), (1668, 2224), (1668, 2388), (2048, 2732),
        # Mac and PC
        (1080, 1920), (1440, 2560), (1600, 2560), (1800, 2880), (1964, 3024), (2234, 3456),
        (1440, 2304), (900, 1440), (768, 1366), (1050, 1680), (1200, 1920),
    }
)  # fmt: skip
"""(short side, long side)."""
MESSAGING_LONG_SIDES = frozenset({800, 960, 1024, 1280, 1600, 2048, 2560, 4096})

PROMPTS: Mapping[str, tuple[str, ...]] = {
    "document": (
        "a photo of a receipt",
        "a scanned document",
        "a page of printed text",
        "a letter or a form",
    ),
    "meme": ("an internet meme with a caption", "a funny picture with words on it"),
    "screenshot": ("a screenshot of a phone screen", "a screenshot of a website"),
    "photo": (
        "a photo of people",
        "a photo of a landscape",
        "a photo of a building",
        "a photo of food",
        "a photo of an animal",
        "a photo of a street with signs",
    ),
}
_LOGIT_SCALE = 100.0


@dataclass(frozen=True)
class Clues:
    name: str
    kind: Kind
    camera: str | None
    has_file_date: bool
    width: int | None
    height: int | None
    flags: frozenset[str]
    text: TextSummary | None
    """Vision's text regions; ``None`` when not measured."""
    bright_fraction: float | None
    labels: Mapping[str, float] | None
    """Zero-shot label probabilities, when there is no text measurement."""


type Scored = tuple[float, dict[str, float]]


def _combine(evidence: Mapping[str, float], factor: float = 1.0) -> float:
    remaining = 1.0
    for weight in evidence.values():
        remaining *= 1 - weight
    return round(min(max((1 - remaining) * factor, 0.0), 1.0), 4)


def _stripped(clues: Clues) -> bool:
    return clues.camera is None and not clues.has_file_date


def _sides(clues: Clues) -> tuple[int, int] | None:
    if not clues.width or not clues.height:
        return None
    return min(clues.width, clues.height), max(clues.width, clues.height)


def _creation(clues: Clues) -> Scored:
    evidence = {}
    if "google-creation" in clues.flags:
        evidence["creation-flag"] = 1.0
    parsed = parse_media_name(clues.name)
    if _CREATION_SUFFIX.search(parsed.edited or parsed.stem):
        evidence["creation-name"] = 0.9
    return _combine(evidence), evidence


def _screenshot(clues: Clues) -> Scored:
    evidence = {}
    if "screenshot" in clues.flags:
        evidence["photos-screenshot"] = 0.95
    if _SCREENSHOT_NAME.search(clues.name):
        evidence["name"] = 0.8
    if clues.kind == "photo" and _sides(clues) in SCREEN_SIZES:
        evidence["screen-size"] = 0.45
    if PurePosixPath(clues.name).suffix.lower() == ".png":
        evidence["png"] = 0.3
    if _stripped(clues):
        evidence["no-camera"] = 0.2
    penalty = 0.25 if clues.camera and "photos-screenshot" not in evidence else 1.0
    return _combine(evidence, penalty), evidence


def _document(clues: Clues) -> Scored:
    evidence: dict[str, float] = {}
    text = clues.text
    if text is not None and text.regions:
        evidence["text-regions"] = round(0.7 * min(1.0, text.regions / 12), 4)
        if text.regions >= 3:  # a page is many lines, not one big sign
            evidence["text-coverage"] = round(0.5 * min(1.0, text.coverage / 0.2), 4)
            if (clues.bright_fraction or 0) >= 0.4:
                evidence["paper"] = 0.3
    elif text is None and clues.labels and clues.labels.get("document"):
        evidence["zero-shot"] = round(0.9 * clues.labels["document"], 4)
    if _stripped(clues) and any(weight >= 0.2 for weight in evidence.values()):
        evidence["no-camera"] = 0.15
    return _combine(evidence), evidence


def _meme(clues: Clues) -> Scored:
    evidence: dict[str, float] = {}
    text = clues.text
    if text is not None and text.regions:
        if text.tallest >= 0.06 and text.banded >= 0.5:
            evidence["caption"] = 0.6
        if 0.02 <= text.coverage <= 0.4:
            evidence["text-overlay"] = 0.2
    elif text is None and clues.labels and clues.labels.get("meme"):
        evidence["zero-shot"] = round(0.8 * clues.labels["meme"], 4)
    if _SAVED.search(clues.name):
        evidence["saved-name"] = 0.3
    if _stripped(clues):
        evidence["no-camera"] = 0.2
    factor = 0.2 if clues.camera else 1.0
    if text is not None and text.regions > 12:
        factor *= 0.5
    return _combine(evidence, factor), evidence


def _forward(clues: Clues) -> Scored:
    evidence = {}
    if _WHATSAPP.search(clues.name):
        evidence["whatsapp-name"] = 0.85
    elif _MESSAGING.search(clues.name):
        evidence["messaging-name"] = 0.6
    if _stripped(clues):
        evidence["stripped"] = 0.2
    sides = _sides(clues)
    if sides is not None and sides[1] in MESSAGING_LONG_SIDES:
        evidence["messaging-size"] = 0.25
    return _combine(evidence, 0.3 if clues.camera else 1.0), evidence


def scores(clues: Clues) -> dict[Category, Scored]:
    return {
        "google-creation": _creation(clues),
        "screenshot": _screenshot(clues),
        "document": _document(clues),
        "meme": _meme(clues),
        "forward": _forward(clues),
    }


def classify(clues: Clues, min_score: float) -> tuple[Category, float, dict[str, float]] | None:
    """The item's best category with its score and evidence, if it reaches ``min_score``."""
    found = scores(clues)
    best = max(CATEGORIES, key=lambda c: (found[c][0], -CATEGORIES.index(c)))
    score, evidence = found[best]
    return (best, score, evidence) if score >= min_score and score > 0 else None


class ZeroShot:
    """open_clip zero-shot labels for a stored embedding (the fallback without Vision text)."""

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
        self._prompts: list[tuple[str, Vector]] | None = None

    def labels(self, vector: Vector) -> dict[str, float]:
        if self._prompts is None:
            texts = [(label, text) for label, texts in PROMPTS.items() for text in texts]
            vectors = self.embedder.embed_texts([text for _, text in texts])
            self._prompts = [(label, v) for (label, _), v in zip(texts, vectors, strict=True)]
        logits = np.array([float(v @ vector) for _, v in self._prompts]) * _LOGIT_SCALE
        weights = np.exp(logits - logits.max())
        weights /= weights.sum()
        found: dict[str, float] = dict.fromkeys(PROMPTS, 0.0)
        for (label, _), weight in zip(self._prompts, weights, strict=True):
            found[label] += float(weight)
        return {label: round(p, 4) for label, p in found.items()}


# --- the stage -----------------------------------------------------------------------------


def _clues(item: CatalogueItem, dates: Iterable[str], zero_shot: ZeroShot | None) -> Clues:
    text = text_from_json(item.text)
    labels = None
    if text is None and item.embedding is not None and zero_shot is not None:
        labels = zero_shot.labels(from_bytes(item.embedding))
    google = item.source == "google"
    measured = json.loads(item.quality)
    return Clues(
        name=PurePosixPath(item.source_key).name if google else item.title or item.source_key,
        kind=item.kind,
        camera=item.camera,
        has_file_date=bool({"exif", "quicktime"} & set(dates)),
        width=item.width,
        height=item.height,
        flags=item.flags,
        text=text,
        bright_fraction=measured.get("bright_fraction"),
        labels=labels,
    )


class ClutterStage:
    """Fill the clutter queue: each item's category, score and evidence, or none."""

    name = "clutter"

    def __init__(self, config: ClutterConfig, zero_shot: ZeroShot | None = None) -> None:
        self.config = config
        self.zero_shot = zero_shot

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        duplicates = {
            item_id
            for group in catalogue.groups("duplicate")
            for item_id, role, _ in catalogue.group_members(group.id)
            if role == "duplicate"
        }
        stored = catalogue.clutter()
        for item in catalogue.items():
            item_id = cast(int, item.id)
            paired_edit = "edited" in item.flags and "unpaired-edit" not in item.flags
            motion = "live-photo-motion" in item.flags
            if item_id in duplicates or (item.source == "google" and (paired_edit or motion)):
                continue
            clues = _clues(item, catalogue.date_candidates(item_id), self.zero_shot)
            found = classify(clues, self.config.min_score)
            if found is None:
                if item_id in stored:
                    yield Action("clear-clutter", item.source_key, {"source": item.source})
                continue
            category, score, evidence = found
            if stored.get(item_id) != (category, score, evidence):
                details: dict[str, JSONValue] = {
                    "source": item.source,
                    "category": category,
                    "score": score,
                    "evidence": cast(dict[str, JSONValue], evidence),
                }
                yield Action("set-clutter", item.source_key, details)

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        source = cast(Source, action.details["source"])
        item_id = catalogue.item_for_key(source, action.target)
        if item_id is None:
            raise KeyError(f"{source}:{action.target} is no longer in the catalogue")
        if action.kind == "clear-clutter":
            catalogue.set_clutter(item_id, None)
            return
        catalogue.set_clutter(
            item_id,
            str(action.details["category"]),
            cast(float, action.details["score"]),
            cast(dict[str, float], action.details["evidence"]),
        )
