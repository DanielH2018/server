"""Fixtures shared by the guards under scripts/tests/."""

import pytest
from _renovate import _tracked_files


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    return _tracked_files()
