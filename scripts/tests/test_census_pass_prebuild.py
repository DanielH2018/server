"""The census rows' shared pass runs before any test's timed phases, by `conftest.py` here.

The two `test_census_rows_test_*.py` files share one pass over the census, and `--durations`
charged it to whichever ran first (#7976). The case runs pytest in a child process, because
only a fresh session shows whether the hits existed before its first test ran. The probe test
it runs, a red/green proof, reads no census module, so the hits can only be there because the
hook found them.

Run: uv run pytest scripts/tests/test_census_pass_prebuild.py
"""

import sys

from _test_module_rules import MODULE_FLOOR
from lib.proc_testing import run
from lib.repo_paths import REPO

# Records, at the first test's setup and ahead of its fixtures, how many modules have hits.
_PROBE = """
import sys
import pytest

seen = []


class Probe:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        seen.append(len(sys.modules["_row_table"]._SHARING.hits))


code = pytest.main([*sys.argv[1:], "-n0", "-p", "no:cacheprovider"], plugins=[Probe()])
print(f"PROBE exit={int(code)} hits={seen[0] if seen else -1}")
"""
_PROBE_TEST = (
    "scripts/tests/test_census_rows_test_modules.py"
    "::test_census_row_flags_its_red_subjects_and_passes_its_green_ones"
    "[tests-run-no-git-write-through-subprocess]"
)


def test_the_census_hits_are_found_before_the_first_test_runs():
    proc = run([sys.executable, "-c", _PROBE, _PROBE_TEST], cwd=REPO, timeout=300)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("PROBE ")]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    exit_code, hits = lines[-1].removeprefix("PROBE exit=").split(" hits=")
    assert exit_code == "0", lines[-1]
    assert int(hits) >= MODULE_FLOOR, lines[-1]
