import os

import pytest
from hypothesis import settings

from photomigrate.testing.takeout import TakeoutManifest, standard_takeout

pytest_plugins = ["pytester", "photomigrate.testing.pytest_guard"]

settings.register_profile("ci", max_examples=300, deadline=None)
settings.register_profile("dev", max_examples=50)
settings.register_profile("stress", max_examples=5000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(scope="session")
def standard_takeout_zips(tmp_path_factory: pytest.TempPathFactory) -> TakeoutManifest:
    """The standard synthetic Takeout (every known quirk) as 3 zip parts. Read-only."""
    return standard_takeout().write_zips(tmp_path_factory.mktemp("takeout"), parts=3)
