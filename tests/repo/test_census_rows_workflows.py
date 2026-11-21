"""The workflow-row table: every census row over the GitHub Actions workflows and actions.

A row that reads one step or job property parses the file through `_workflow_rows.judge`, and
its red half is the real file with that property broken (`_workflow_rows.mutants`). The rows
sit in `_workflow_rows.py` and the `_*_workflow_rows.py` modules beside it, one per workflow
family, because `TEST_CAP` in `_ratchet.py` caps any one module.

Run: uv run pytest tests/repo/test_census_rows_workflows.py
"""

import pytest
from _ci_handoff_workflow_rows import HANDOFF_ROWS
from _ci_job_rows import CI_JOB_ROWS
from _ci_script_cache_row import SCRIPT_DOWNLOADS_ARE_CACHED
from _ci_k3s_download_rows import K3S_DOWNLOAD_ROWS
from _ci_sweep_workflow_rows import SWEEP_ROWS
from _ci_workflow_rows import CI_ROWS
from _k3s_download_leg_row import K3S_DOWNLOAD_LEG_DOES_NOT_GATE
from _promote_render_workflow_rows import PROMOTE_RENDER_ROWS
from _promote_verify_workflow_rows import PROMOTE_VERIFY_ROWS
from _promote_workflow_rows import PROMOTE_ROWS
from _pytest_sweep_image_row import PYTEST_SWEEP_RUNS_THE_IMAGE_TESTS
from _pytest_sweep_workflow_rows import PYTEST_SWEEP_ROWS
from _prune_workflow_rows import PRUNE_ROWS
from _row_table import Census, check, proof_problems
from _setup_action_rows import SETUP_ACTION_ROWS
from _workflow_rows import WORKFLOW_ROWS

ROWS = (
    *WORKFLOW_ROWS,
    *PROMOTE_ROWS,
    *PROMOTE_RENDER_ROWS,
    *PROMOTE_VERIFY_ROWS,
    *PRUNE_ROWS,
    *CI_ROWS,
    *HANDOFF_ROWS,
    *CI_JOB_ROWS,
    *K3S_DOWNLOAD_ROWS,
    *SWEEP_ROWS,
    *PYTEST_SWEEP_ROWS,
    PYTEST_SWEEP_RUNS_THE_IMAGE_TESTS,
    K3S_DOWNLOAD_LEG_DOES_NOT_GATE,
    SCRIPT_DOWNLOADS_ARE_CACHED,
    *SETUP_ACTION_ROWS,
)
_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    problems = proof_problems(row)
    assert not problems, "\n".join(problems)
