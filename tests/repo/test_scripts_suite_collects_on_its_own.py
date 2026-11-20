#!/usr/bin/env python3
"""Guard that `uv run pytest scripts` collects without help from another testpath.

A whole-suite run can pass while one suite alone fails to collect, when another testpath's
conftest supplies a name the first one imports. `scripts/renderer/job_files/flux_soak.py`
imports `changed_set` by bare name, the way the soak Job's flat mount lays it out, and only
`scripts/renderer/tests/conftest.py` binds that name.

The red proof: drop the `changed_set` binding from that conftest and this fails.
"""

from _helpers import REPO
from lib.proc_testing import run


def test_scripts_testpath_collects_in_a_fresh_interpreter():
    # -n0 so this does not spawn xdist workers inside the parent run.
    result = run(
        ["uv", "run", "pytest", "--collect-only", "-n0", "-q", "scripts"],
        cwd=REPO,
        # A nested `uv run` resolves the dev group before the child starts, which a cold
        # cache makes minutes rather than seconds — longer than `DEFAULT_TIMEOUT` allows.
        timeout=300,
    )
    assert result.returncode == 0, (
        "`uv run pytest scripts` failed to collect on its own — a test there imports a "
        f"name only another testpath puts on sys.path:\n{result.stdout}\n{result.stderr}"
    )
    # Non-vacuity: a collection that found nothing also exits 0.
    assert "test_flux_soak.py" in result.stdout, (
        f"collection did not reach the module this guard exists for:\n{result.stdout}"
    )
