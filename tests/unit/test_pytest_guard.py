"""The session-wide safety guard plugin (CLAUDE.md rule 4) fails tests that touch real media.

Each test runs an inner pytest session with the plugin loaded and makes a deliberate access
attempt. The guard blocks the access before the operating system sees it.
"""

from pathlib import Path

import pytest

PLUGIN = ("-p", "photomigrate.testing.pytest_guard", "-p", "no:cacheprovider")


def run(pytester: pytest.Pytester) -> pytest.RunResult:
    return pytester.runpytest(*PLUGIN)


def test_opening_a_file_in_pictures_fails_the_test(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        from pathlib import Path

        def test_peek():
            (Path.home() / "Pictures" / "photomigrate-guard-probe.jpg").open("rb")
        """
    )

    result = run(pytester)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*safety guard*photomigrate-guard-probe.jpg*"])


def test_opening_a_photos_library_outside_tmp_fails_the_test(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        def test_peek():
            open("/Volumes/photomigrate-probe/Old.photoslibrary/database/Photos.sqlite", "rb")
        """
    )

    run(pytester).assert_outcomes(failed=1)


def test_a_throwaway_library_in_slash_tmp_is_allowed(pytester: pytest.Pytester) -> None:
    """macOS's temp dir is /var/folders/…/T, but /tmp is just as temporary (and where a
    throwaway Photos library for the opt-in Photos.app test is easiest to create)."""
    pytester.makepyfile(
        """
        import os, shutil, uuid
        def test_peek():
            library = f"/tmp/photomigrate-guard-{uuid.uuid4().hex}.photoslibrary"
            os.makedirs(library)
            try:
                open(os.path.join(library, "x"), "wb").close()
            finally:
                shutil.rmtree(library)
        """
    )

    run(pytester).assert_outcomes(passed=1)


def test_opening_a_real_takeout_archive_fails_the_test(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        from pathlib import Path

        def test_peek():
            (Path.home() / "Downloads" / "takeout-photomigrate-guard-probe.zip").open("rb")
        """
    )

    run(pytester).assert_outcomes(failed=1)


def test_swallowing_the_error_does_not_hide_the_access(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        from pathlib import Path

        def test_sneaky():
            try:
                (Path.home() / "Pictures" / "photomigrate-guard-probe.jpg").open("rb")
            except PermissionError:
                pass
        """
    )

    result = run(pytester)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*safety guard*open*photomigrate-guard-probe.jpg*"])


def test_access_from_a_fixture_errors_the_test(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import subprocess
        from pathlib import Path
        import pytest

        @pytest.fixture
        def peek():
            yield
            try:
                probe = Path.home() / "Pictures" / "photomigrate-guard-probe"
                subprocess.run(["ls", str(probe)], check=False)
            except PermissionError:
                pass

        def test_uses_fixture(peek):
            pass
        """
    )

    run(pytester).assert_outcomes(passed=1, errors=1)


def test_scratch_libraries_and_takeouts_under_tmp_are_allowed(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        def test_fixture_media(tmp_path):
            library = tmp_path / "Disposable.photoslibrary" / "database"
            library.mkdir(parents=True)
            (library / "Photos.sqlite").write_bytes(b"")
            takeout = tmp_path / "Takeout" / "Google Photos"
            takeout.mkdir(parents=True)
            (takeout / "IMG_0001.jpg.json").write_text("{}")
        """
    )

    run(pytester).assert_outcomes(passed=1)


def test_extra_forbidden_paths_come_from_the_environment(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_export = tmp_path / "real-export"
    real_export.mkdir()
    (real_export / "IMG_0001.jpg").write_bytes(b"")
    monkeypatch.setenv("PHOTOMIGRATE_GUARD_FORBID", str(real_export))
    pytester.makepyfile(
        f"""
        def test_peek():
            open({str(real_export / "IMG_0001.jpg")!r}, "rb")
        """
    )

    run(pytester).assert_outcomes(failed=1)


def test_access_outside_any_test_still_fails_the_session(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
        from pathlib import Path

        def pytest_collection_finish(session):
            try:
                (Path.home() / "Pictures" / "photomigrate-guard-probe.jpg").open("rb")
            except PermissionError:
                pass
        """
    )
    pytester.makepyfile("def test_ok(): pass")

    result = run(pytester)

    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*photomigrate safety guard*", "*blocked open*probe.jpg*"])


def test_the_real_samples_folder_is_off_limits(pytester: pytest.Pytester) -> None:
    samples = pytester.path / "data-in"
    samples.mkdir()
    (samples / "Timeline.json").write_text("[]")
    test_file = pytester.makepyfile(
        """
        def test_peek(pytestconfig):
            (pytestconfig.rootpath / "data-in" / "Timeline.json").open("rb")
        """
    )

    # Name the file: collecting the whole rootdir would already trip the guard on data-in/.
    pytester.runpytest(*PLUGIN, str(test_file)).assert_outcomes(failed=1)
