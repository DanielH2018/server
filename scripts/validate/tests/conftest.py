"""Keeps this directory's subprocess tests out of the host's syslog.

The shell-template tests run the rendered backup-health shim for real, and the shim calls
`logger` on its failure paths. This puts a stubbed `logger` first on PATH, from
`_helpers.stub_logger_on_path`, autouse and directory-wide rather than opt-in per test: a
later test that runs another rendered cron would otherwise start polluting silently.
`test_backup_health_shim.py::test_backup_health_reader_failure_is_logged_through_the_stub` is
the proof the stub is on PATH.
"""

import pytest
from _helpers import stub_logger_on_path


@pytest.fixture(autouse=True)
def _no_syslog(tmp_path_factory, monkeypatch):
    return stub_logger_on_path(tmp_path_factory, monkeypatch)


@pytest.fixture
def logger_calls(_no_syslog):
    """The file the stubbed `logger` appends to, one line per call: `-t <tag> <message>`."""
    return _no_syslog
