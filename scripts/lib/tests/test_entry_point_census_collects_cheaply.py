"""Collecting the `--help` census modules costs under a second each (#7730).

`test_entry_points_answer_help.py` classified every script at import, and
`test_help_check_rejects_work_output.py` imports it, so each module's collection cost 4.5s
serially and 7-8s per xdist worker in CI. That time falls outside every `--durations` phase
and every shard weight. Each module here is collected in its own pytest subprocess, so the
second cannot ride on the first's import, and the cost is read from the `phase_timing`
plugin's report, which lists every module collection of 0.2s or more.

Run: uv run pytest scripts/lib/tests/test_entry_point_census_collects_cheaply.py
"""

import os
import re
import subprocess
import sys

import pytest

from lib.repo_paths import REPO

BUDGET_SECONDS = 1.0

CENSUS = "scripts/lib/tests/test_entry_points_answer_help.py"
CENSUS_IMPORTER = "scripts/lib/tests/test_help_check_rejects_work_output.py"

# A test id each module must still collect, so a fix that drops the parametrize, or a run that
# collected nothing, cannot pass on speed alone. The census needs its entry-point names at
# collection, and `deploy.sh` is the entry point whose answer the census was written for.
MUST_COLLECT = {
    CENSUS: f"{CENSUS}::test_the_entry_point_answers_help_with_exit_zero[deploy.sh]",
    CENSUS_IMPORTER: (
        f"{CENSUS_IMPORTER}::"
        "test_the_help_check_refuses_an_entry_point_that_runs_its_job"
    ),
}

_COLLECTION = re.compile(r"^\s*collect\s+([0-9.]+)s\s+(\S+)\s*$", re.M)


def _collect(module: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            module,
            "-n",
            "0",
            "--collect-only",
            "-q",
            "--durations=0",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    return done.stdout


@pytest.mark.parametrize("module", [CENSUS, CENSUS_IMPORTER], ids=os.path.basename)
def test_the_module_collects_in_under_a_second(module):
    out = _collect(module)
    assert MUST_COLLECT[module] in out.splitlines(), out[-2000:]
    assert "phase timing" in out, f"the phase_timing report is missing:\n{out[-2000:]}"
    seconds = {name: float(s) for s, name in _COLLECTION.findall(out)}.get(module, 0.0)
    assert seconds < BUDGET_SECONDS, (
        f"{module} took {seconds:.2f}s to collect; its import must not classify every "
        f"script (#7730)\n{out[-1000:]}"
    )
