"""Static check: production code never calls a delete or trash API (CLAUDE.md rule 3)."""

from pathlib import Path

import pytest

import photomigrate
from photomigrate.safety import find_delete_calls, main, scan_paths


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.remove(p)",
        "import os\nos.unlink(p)",
        "import os\nos.rmdir(p)",
        "import os\nos.removedirs(p)",
        "import shutil\nshutil.rmtree(d)",
        "from pathlib import Path\nPath(p).unlink()",
        "photo.delete()",
        "library.delete_photo(p)",
        "send2trash(p)",
        "import send2trash",
        "from send2trash import send2trash",
        "from os import remove as rm",
        "from shutil import rmtree",
        "PHAssetChangeRequest.deleteAssets_(assets)",
        "fm.trashItemAtURL_resultingItemURL_error_(url, None, None)",
        "fm.removeItemAtPath_error_(path, None)",
        "library.removeAssets_(assets)",
    ],
)
def test_delete_apis_are_flagged(source: str) -> None:
    findings = find_delete_calls(source, "mod.py")

    assert len(findings) == 1
    assert findings[0].path == "mod.py"
    assert findings[0].line == source.count("\n") + 1


@pytest.mark.parametrize(
    "source",
    [
        "items.remove(x)",
        "album = 'To Delete'",
        "from photomigrate.importer import todelete\ntodelete.stage(items)",
        "stage_to_delete(items)",
        "album.addToDelete(items)",
        "cur.execute('DELETE FROM decisions WHERE run_id = ?', (run,))",
        "(lambda: None)()",
        "from . import sibling",
        "import os.path\nos.path.join(a, b)",
    ],
)
def test_harmless_code_is_not_flagged(source: str) -> None:
    assert find_delete_calls(source) == []


def test_finding_message_names_location_and_rule() -> None:
    (finding,) = find_delete_calls("x = 1\nshutil.rmtree(d)\n", "src/mod.py")

    assert str(finding) == "src/mod.py:2: delete API 'shutil.rmtree' is forbidden (CLAUDE.md rule 3)"


def test_scan_paths_walks_directories_and_files(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "clean.py").write_text("print('hi')\n")
    (pkg / "sub" / "bad.py").write_text("import os\nos.remove('x')\n")
    single = tmp_path / "single.py"
    single.write_text("p.unlink()\n")

    findings = scan_paths([pkg, single])

    assert [(Path(f.path).name, f.line) for f in findings] == [("bad.py", 2), ("single.py", 1)]


def test_production_package_has_no_delete_calls() -> None:
    assert scan_paths([Path(photomigrate.__file__).parent]) == []


def test_main_fails_and_reports_findings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "bad.py").write_text("import shutil\nshutil.rmtree('x')\n")

    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "bad.py:2: delete API 'shutil.rmtree'" in out
    assert "FAILED: 1 forbidden call" in out


def test_main_passes_on_clean_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "ok.py").write_text("print('hi')\n")

    assert main([str(tmp_path)]) == 0
    assert "no-delete check passed" in capsys.readouterr().out


def test_main_defaults_to_src_from_sys_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bad.py").write_text("p.unlink()\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["photomigrate-no-delete-check"])

    assert main() == 1
    assert "bad.py:1" in capsys.readouterr().out
