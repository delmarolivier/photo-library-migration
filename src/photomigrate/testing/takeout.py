"""Synthetic Google Takeout exports for tests (M1-02).

The naming rules follow docs/TAKEOUT_FORMAT.md. Rules seen in Delmar's real sample:
``.supplemental-metadata.json`` sidecars, ``(N)`` moving to just before ``.json``, and
``-edited`` copies without their own sidecar. Rules modelled on public reports (the
google-photos-takeout-helper project) and not in the sample: legacy ``.json`` sidecars,
47-character media names, 51-character sidecar names, and localised edit suffixes. If a real
export disagrees, fix the model here and in the doc.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

SidecarStyle = Literal["supplemental", "legacy"]

MEDIA_NAME_LIMIT = 47
SIDECAR_NAME_LIMIT = 51
SUPPLEMENTAL_SUFFIX = ".supplemental-metadata"

EDITED_SUFFIXES = {
    "en": "-edited",
    "fr": "-modifié",
    "de": "-bearbeitet",
    "it": "-modificato",
    "nl": "-bewerkt",
    "pl": "-edytowane",
    "ja": "-編集済み",
}


def _split(name: str) -> tuple[str, str]:
    suffix = PurePosixPath(name).suffix
    return name[: len(name) - len(suffix)], suffix


def truncate_media_name(name: str) -> str:
    """Cut a media file name to 47 characters, keeping its extension."""
    stem, ext = _split(name)
    return stem[: MEDIA_NAME_LIMIT - len(ext)] + ext


def duplicate_name(name: str, index: int) -> str:
    """``IMG_1234.JPG`` with index 1 becomes ``IMG_1234(1).JPG``."""
    if not index:
        return name
    stem, ext = _split(name)
    return f"{stem}({index}){ext}"


def edited_name(
    name: str, *, lang: str = "en", extension: str | None = None, index: int = 0
) -> str:
    """Name of the edited copy of the media file ``name`` (its on-disk name, without index)."""
    stem, ext = _split(name)
    edited = truncate_media_name(stem + EDITED_SUFFIXES[lang] + (extension or ext))
    return duplicate_name(edited, index)


def sidecar_name(media_name: str, *, style: SidecarStyle = "supplemental", index: int = 0) -> str:
    """Sidecar for the on-disk media name ``media_name`` (without its ``(N)`` index).

    The name before ``.json`` is cut so the whole name fits in 51 characters, which trims the
    supplemental suffix first and then the media name itself (``IMG….jp.json``).
    """
    suffix = SUPPLEMENTAL_SUFFIX if style == "supplemental" else ""
    base = (media_name + suffix)[: SIDECAR_NAME_LIMIT - len(".json")]
    return f"{base}({index}).json" if index else f"{base}.json"
