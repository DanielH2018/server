"""Fixtures for the deploy-ui daemon tests. `files/` is on pythonpath via pyproject."""

import pathlib

import pytest


@pytest.fixture
def state_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "state"
    d.mkdir()
    return d
