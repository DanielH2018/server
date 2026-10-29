#!/usr/bin/env python3
"""Every series a host job writes through the textfile collector is on a provisioned board.

The host jobs report through node_exporter's textfile directory rather than a scrape target of
their own, and the rules in `files/rules/host-checks.yaml` read their series. Until #6203 no
board did, so a check that sat red or late was visible only as a page. A board file that
`observability_dashboard_folders` does not list is provisioned nowhere, so the census reads
the boards through that map rather than a glob.

Run: uv run pytest ansible/tests/services/test_host_job_series_have_a_board.py
"""

import json
from pathlib import Path

from _helpers import ANSIBLE
from _helpers import load_yaml

ROLE = ANSIBLE / "roles" / "k8s" / "observability"
DASHBOARDS_DIR = ROLE / "files" / "dashboards"

# The textfile checks' contract (homelab-check-lib.sh), the builder's signer
# (hosts/lib/homelab_repo_sign.py) and the updater (hosts/lib/homelab-updater).
HOST_JOB_SERIES = frozenset(
    {
        "homelab_check_success",
        "homelab_check_last_run_timestamp_seconds",
        "homelab_check_deadline_seconds",
        "homelab_repo_published_info",
        "homelab_host_reconcile_ok",
        "homelab_host_applied_info",
        "homelab_host_update_success_timestamp_seconds",
    }
)


def provisioned_exprs(dashboards_dir: Path, folders: dict[str, list[str]]) -> list[str]:
    """Every panel query of every board the folder map provisions."""
    exprs = []
    for folder, boards in folders.items():
        for board in boards:
            data = json.loads((dashboards_dir / folder / board).read_text())
            for panel in data.get("panels", []):
                exprs += [t.get("expr", "") for t in panel.get("targets", [])]
    return exprs


def unboarded(series: frozenset[str], exprs: list[str]) -> set[str]:
    """The series no query names."""
    return {name for name in series if not any(name in expr for expr in exprs)}


def test_every_host_job_series_is_on_a_provisioned_board():
    folders = load_yaml(ROLE / "defaults" / "main.yml")[
        "observability_dashboard_folders"
    ]
    exprs = provisioned_exprs(DASHBOARDS_DIR, folders)
    assert unboarded(HOST_JOB_SERIES, exprs) == set()


def test_a_series_no_board_queries_is_flagged(tmp_path):
    (tmp_path / "Infra").mkdir()
    board = {"panels": [{"targets": [{"expr": "max(homelab_check_success)"}]}]}
    (tmp_path / "Infra" / "b.json").write_text(json.dumps(board))
    (tmp_path / "Infra" / "unlisted.json").write_text(
        json.dumps({"panels": [{"targets": [{"expr": "homelab_host_reconcile_ok"}]}]})
    )

    exprs = provisioned_exprs(tmp_path, {"Infra": ["b.json"]})

    assert unboarded(
        frozenset({"homelab_check_success", "homelab_host_reconcile_ok"}), exprs
    ) == {"homelab_host_reconcile_ok"}
