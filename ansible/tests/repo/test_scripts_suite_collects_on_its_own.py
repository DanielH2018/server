#!/usr/bin/env python3
"""Guard that `uv run pytest scripts` collects without help from another testpath.

CLAUDE.md's Python & Tests section documents scoping to one suite, and for months
`scripts/diagnostics/tests/test_grafana_panel_report.py` imported `grafana_panel_report`
by bare name with nothing under `scripts/` resolving it. The whole-suite run passed only
because collecting `ansible/tests/setup/test_pi_health_log_line_shape.py` put
`scripts/diagnostics` on `sys.path` first. Reorder collection and two unrelated modules
stop importing.

The red proof is the fix itself: drop the `grafana_panel_report` load from
`scripts/conftest.py` and this fails. The green proof is that it passes now.
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
    assert "test_grafana_panel_report.py" in result.stdout, (
        f"collection did not reach the module this guard exists for:\n{result.stdout}"
    )
