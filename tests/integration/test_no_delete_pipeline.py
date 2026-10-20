"""The whole pipeline, with every delete API rigged to fail on media (CLAUDE.md rule 3).

Deleting functions raise if pointed at the Takeout or the library, Photos' own delete
commands raise whenever called, and a ``NoDeleteGuard`` watches both folders through the
audit hook. Ingest, analyse, a review, the import and the To Delete staging then run end to
end. Scratch files elsewhere (working copies, temporary folders) may still be cleaned up.
"""

import hashlib
import json
import os
import pathlib
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from photomigrate.importer.stage import FROM_GOOGLE, TO_DELETE
from photomigrate.review.queues import QUEUES
from photomigrate.safety import NoDeleteGuard
from photomigrate.testing.photos import DisposableLibrary
from photomigrate.testing.takeout import TakeoutManifest


class DeleteAttemptError(AssertionError):
    pass


def rig(monkeypatch: pytest.MonkeyPatch, protected: list[Path]) -> list[str]:
    """Make every delete API fail on ``protected`` paths; record each refusal."""
    refused: list[str] = []
    roots = [os.path.realpath(p) for p in protected]

    def guarded(name: str, original: Any) -> Any:
        def wrapper(target: Any, *args: Any, **kwargs: Any) -> Any:
            path = os.path.realpath(os.fspath(target))
            if any(path == r or path.startswith(r + os.sep) for r in roots):
                refused.append(f"{name}({path})")
                raise DeleteAttemptError(f"{name} on media: {path}")
            return original(target, *args, **kwargs)

        return wrapper

    for module, name in [(os, "remove"), (os, "unlink"), (os, "rmdir"), (os, "removedirs"),
                         (shutil, "rmtree")]:  # fmt: skip
        monkeypatch.setattr(
            module, name, guarded(f"{module.__name__}.{name}", getattr(module, name))
        )
    for name in ("unlink", "rmdir"):
        method = getattr(pathlib.Path, name)
        monkeypatch.setattr(
            pathlib.Path,
            name,
            lambda self, *a, _m=method, _n=name, **k: guarded(
                f"Path.{_n}", lambda p, *aa, **kk: _m(pathlib.Path(p), *aa, **kk)
            )(self, *a, **k),
        )
    if sys.platform == "darwin":
        import photoscript

        def never(*args: Any, **kwargs: Any) -> None:
            raise DeleteAttemptError("a Photos delete command was called")

        for name in ("delete_album", "delete_folder"):
            monkeypatch.setattr(photoscript.PhotosLibrary, name, never)
    return refused


def digest(folder: Path) -> dict[str, str]:
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*")) if p.is_file()
    }  # fmt: skip


def test_the_whole_pipeline_runs_without_deleting_anything(
    standard_takeout_zips: TakeoutManifest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeout = standard_takeout_zips.zips[0].parent
    library = DisposableLibrary(tmp_path / "Test.photoslibrary")
    catalogue_path = tmp_path / "catalogue.sqlite"
    zips_before = digest(takeout)
    refused = rig(monkeypatch, [takeout, library.root])
    monkeypatch.setattr("photomigrate.cli.photos_library", lambda path: library)
    common = ["--catalogue", str(catalogue_path), "--plan-dir", str(tmp_path / "plans")]
    runner = CliRunner()

    with NoDeleteGuard([takeout, library.root]) as guard:
        for args in (
            ["ingest", str(takeout), "--apply", "--work-dir", str(tmp_path / "work"),
             "--timezone", "Europe/Paris"],
            ["analyse", "--apply"],
        ):  # fmt: skip
            result = runner.invoke(app, [*common, *args])
            assert result.exit_code == 0, result.output
        with Catalogue.open(catalogue_path) as catalogue:
            existing = catalogue.upsert_item(
                CatalogueItem(source="icloud", source_key="UUID-OLD", kind="photo", title="old.jpg")
            )
            catalogue.set_date_candidate(existing, "icloud", datetime(2020, 1, 1, tzinfo=UTC))
            library.add_existing("UUID-OLD", "old.jpg")
            catalogue.set_decision("item", existing, "quality", {"reject": True})
            for queue in QUEUES.values():  # accept every proposal, as a quick review would
                for card in queue.cards(catalogue):
                    if card.decision is None and card.kind not in ("date", "quality"):
                        catalogue.set_decision(card.subject, card.subject_id, card.kind,
                                               card.proposal.value)  # type: ignore[arg-type]  # fmt: skip
        marker = tmp_path / "BACKUP-CONFIRMED"
        confirmed = runner.invoke(
            app, ["confirm-backup", "--backup-marker", str(marker)], input="y\n"
        )
        assert confirmed.exit_code == 0, confirmed.output
        imported = runner.invoke(
            app,
            [*common, "import", "--takeout", str(takeout), "--library", str(library.root),
             "--work-dir", str(tmp_path / "work"), "--timezone", "Europe/Paris",
             "--backup-marker", str(marker), "--apply"],
            input="y\n",
        )  # fmt: skip
        assert imported.exit_code == 0, imported.output
        reported = runner.invoke(app, [*common, "report", "--out", str(tmp_path / "report")])
        assert reported.exit_code == 0, reported.output
        assert "0 unaccounted" in reported.output

    assert refused == []
    assert guard.violations == []
    assert digest(takeout) == zips_before, "the Takeout is byte for byte the same"
    state = json.loads((library.root / "library.json").read_text(encoding="utf-8"))
    assert "UUID-OLD" in state["assets"], "a rejected iCloud item is staged, not deleted"
    assert state["albums"][TO_DELETE] == ["UUID-OLD"]
    assert len(state["albums"][FROM_GOOGLE]) > 10


def test_the_rigged_apis_do_refuse_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.jpg").write_bytes(b"x")
    scratch = tmp_path / "scratch.txt"
    scratch.write_bytes(b"x")
    refused = rig(monkeypatch, [media])

    for attempt in (lambda: os.remove(media / "a.jpg"), lambda: (media / "a.jpg").unlink(),
                    lambda: shutil.rmtree(media)):  # fmt: skip
        with pytest.raises(DeleteAttemptError):
            attempt()
    scratch.unlink()

    assert len(refused) == 3
    assert (media / "a.jpg").exists()
