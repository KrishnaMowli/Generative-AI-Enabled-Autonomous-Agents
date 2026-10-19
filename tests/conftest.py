import pytest

from autonomous_collab.config import load_settings


@pytest.fixture(scope="session")
def settings():
    return load_settings()
