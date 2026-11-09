"""No provisioned board reads the retired GitOps tick's `gitops_tick_*` series.

`gitops_tick_metrics.py` wrote those series on every tick of `gitops-deploy.timer`, and H7.52
retired the timer, so a panel reading them stays empty for good (#7825).

Run: uv run pytest ansible/tests/services/test_dashboards_read_no_gitops_tick_series.py
"""

from _helpers import REPO
from _row_table import tracked

DASHBOARDS = "ansible/roles/k8s/observability/files/dashboards"
RETIRED_PREFIX = "gitops_tick_"


def test_no_dashboard_names_a_gitops_tick_series():
    boards = tracked(f"{DASHBOARDS}/*.json")
    assert boards, f"no board tracked under {DASHBOARDS}"
    offenders = [
        f"{rel}:{n}"
        for rel in boards
        for n, line in enumerate((REPO / rel).read_text().splitlines(), 1)
        if RETIRED_PREFIX in line
    ]
    assert offenders == []
