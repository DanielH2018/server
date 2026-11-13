"""Fixtures shared by the guards under scripts/tests/."""

import sys

import pytest
from _renovate import _tracked_files


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    return _tracked_files()


# DECIDED: the census pass leaves `--durations`, as the k8s render does in
# `tests/conftest.py`. Both `test_census_rows_test_*.py` files share one per-process pass
# over about 1600 modules, and the first to run paid 8-11s of it while its sibling paid 0.05s.
# The serial `pytest_shard.py --record` charged it to one file and CI measured it on the other,
# which failed the shard-weight gate (#7976). `pytest_shard.py --summary` charges it per shard.
def pytest_collection_finish(session: pytest.Session) -> None:
    """Run the shared census rows' pass once per process, before any test's timed phases.

    The module is looked up, not imported, and it builds nothing unless a census file shared
    its rows at import: a run that collected neither file pays nothing.
    """
    if session.config.option.collectonly:
        return
    if (row_table := sys.modules.get("_row_table")) is not None:
        row_table.prebuild_shared_rows()
