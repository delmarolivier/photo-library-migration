import os

from hypothesis import settings

pytest_plugins = ["pytester", "photomigrate.testing.pytest_guard"]

settings.register_profile("ci", max_examples=300, deadline=None)
settings.register_profile("dev", max_examples=50)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
