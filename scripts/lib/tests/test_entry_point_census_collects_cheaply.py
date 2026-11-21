"""Importing the `--help` census modules costs under a second of CPU each.

`test_entry_points_answer_help.py` classified every script at import, and
`test_help_check_rejects_work_output.py` imports it, so each module's collection cost 4.5s
serially and 7-8s per xdist worker in CI. That time falls outside every `--durations` phase
and every shard weight. Each module is imported in its own interpreter, so the second cannot
ride on the first's import. The import is timed in CPU seconds: a loaded xdist worker
stretches wall time on a module whose import did not change, and CPU time does not stretch.

Run: uv run pytest scripts/lib/tests/test_entry_point_census_collects_cheaply.py
"""

import os
import subprocess
import sys

import pytest

from lib.repo_paths import REPO, SCRIPTS

BUDGET_CPU_SECONDS = 1.0

CENSUS = "scripts/lib/tests/test_entry_points_answer_help.py"
CENSUS_IMPORTER = "scripts/lib/tests/test_help_check_rejects_work_output.py"

# A test id each module must still collect, so a fix that drops the parametrize, or a run that
# collected nothing, cannot pass on speed alone. The census needs its entry-point names at
# collection, and `deploy.sh` is the entry point whose answer the census was written for.
MUST_COLLECT = {
    CENSUS: f"{CENSUS}::test_the_entry_point_answers_help_with_exit_zero[land.sh]",
    CENSUS_IMPORTER: (
        f"{CENSUS_IMPORTER}::"
        "test_the_help_check_refuses_an_entry_point_that_runs_its_job"
    ),
}

IMPORT_CPU_SECONDS = """
import importlib, sys, time
sys.path[:0] = sys.argv[2:]
start = time.process_time()
importlib.import_module(sys.argv[1])
print(time.process_time() - start)
"""


def _run(argv: list[str], env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        argv, cwd=REPO, env=env, capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    return done.stdout


def _collected_ids(module: str) -> list[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    argv = [sys.executable, "-m", "pytest", module, "-n", "0", "--collect-only", "-q"]
    return _run(argv, env).splitlines()


def _import_cpu_seconds(module: str) -> float:
    path = REPO / module
    argv = [sys.executable, "-c", IMPORT_CPU_SECONDS, path.stem, str(SCRIPTS)]
    return float(_run([*argv, str(path.parent)]).splitlines()[-1])


@pytest.mark.parametrize("module", [CENSUS, CENSUS_IMPORTER], ids=os.path.basename)
def test_the_module_collects_in_under_a_second(module):
    assert MUST_COLLECT[module] in _collected_ids(module)
    seconds = _import_cpu_seconds(module)
    assert seconds < BUDGET_CPU_SECONDS, (
        f"{module} took {seconds:.2f} CPU seconds to import; its import must not "
        f"classify every script (#7730)"
    )
