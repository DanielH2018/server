"""Keeps this directory's subprocess tests out of the host's syslog.

Every test here that runs a host script for real (the backup-health reader, the reapers) gets
a stubbed `logger` first on PATH, from `_helpers.stub_logger_on_path`. Autouse and
directory-wide rather than opt-in per test: the set of tests that spawn a real script is not
closed, and one added later would silently start polluting again. A stubbed `logger` no test
calls costs nothing.
`test_longhorn_backup_health_reader.py::test_reader_syslog_line_is_intercepted` is the proof the
stub is on PATH.
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
