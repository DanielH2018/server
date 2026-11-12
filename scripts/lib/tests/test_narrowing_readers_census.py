"""The narrowing's census row selects the two setup-role readers (#7297).

The row in `test_yaml_fast.py` refuses a direct parse in whatever it selects, so selection is
the whole claim here. This file is separate because the fix edits `test_yaml_fast.py`.

Run: uv run pytest scripts/lib/tests/test_narrowing_readers_census.py
"""

import importlib

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "scripts/deploy_tools/setup_lib/setup_gates.py",
        "scripts/deploy_tools/setup_lib/setup_role_chains.py",
    ],
)
def test_the_narrowing_census_row_selects_the_reader(path):
    census = importlib.import_module("test_yaml_fast").NARROWING_CENSUS
    assert path in census.files()
