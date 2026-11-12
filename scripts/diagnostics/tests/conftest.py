"""Shared fixtures for the diagnostics tests.

Run: uv run pytest scripts/diagnostics/tests
"""

import pytest

from land_lib.detach import LOG_DIR_ENV


@pytest.fixture(autouse=True)
def _empty_landing_log_dir(monkeypatch, tmp_path):
    """Point `default_log_dir()` at an empty directory instead of the host's landing logs.

    `landing.collect` reads the newest `land*.log` there for its `Last landing:` line, and no
    test passes a fake for that read, so on a host that has landed a test would assert against
    that log's VERDICT.
    """
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
