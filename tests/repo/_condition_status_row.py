"""The condition-status census row: every shipped module compares a condition's status as an enum.

The row sits in its own module because `test_census_rows_python.py`, which imports it into
`ROWS`, is at the cap for a test module.
"""

import re

from _helpers import REPO
from _row_table import Census, Subject, lines_matching, tracked


def _python_sources() -> list[str]:
    """Every tracked Python module under `scripts/`, `hosts/` and `k8s/roles/`, tests aside.

    A host script installed by its bare name, such as `hosts/lib/homelab-k3s-restart`, has no
    suffix, so its shebang marks it.
    """
    suffixed = tracked("scripts/*.py", "hosts/*.py", "k8s/roles/*.py")
    bare = [
        rel
        for rel in tracked("hosts/*")
        if "." not in rel.rsplit("/", 1)[-1]
        and (REPO / rel).read_bytes().startswith(b"#!/usr/bin/env python3")
    ]
    return sorted(rel for rel in {*suffixed, *bare} if "/tests/" not in rel)


CONDITION_STATUS_IS_PARSED = Census(
    name="scripts-read-condition-status-through-the-shared-parser",
    reason=(
        "A condition's status is `True`, `False` or `Unknown`, and a hand-written compare "
        "against the string reads a misspelt literal as a condition that never holds. "
        "`lib.k8s_conditions.parse_conditions` reads `.status.conditions` into a "
        "`ConditionStatus`; compare with `is ConditionStatus.TRUE`. Every module under "
        "`scripts/`, `hosts/` and `k8s/roles/` outside the tests is in scope. A host script or "
        "a role's Job that cannot import the checkout ships `scripts/lib/k8s_conditions.py` "
        "under a `lib/` beside it, as `k3s-gates` and configarr's health ConfigMap do."
    ),
    files=_python_sources,
    offence=lines_matching(
        re.compile(
            r"""(==|!=)\s*["'](True|False|Unknown)["']|["'](True|False|Unknown)["']\s*(==|!=)"""
        )
    ),
    red=(
        Subject("a.py", 'ready = cond.get("status") == "True"\n'),
        Subject("b.py", "if 'False' != status:\n"),
    ),
    green=(
        Subject("a.py", "ready = cond.status is ConditionStatus.TRUE\n"),
        Subject("b.py", '"""Ready reads `True` once the pod is up."""\n'),
    ),
    min_matches=50,
    must_find=frozenset(
        {
            "hosts/components/claude-code/files/claude_restart.py",
            "hosts/components/hypervisor/files/suc_rehearsal.py",
            "hosts/lib/homelab-k3s-restart",
            "hosts/lib/k3s_gates.py",
            "k8s/roles/configarr/files/configarr_lib/configarr_health_logic.py",
            "scripts/deploy_tools/bootstrap_drill.py",
            "scripts/deploy_tools/fresh_cluster_drill.py",
            "scripts/deploy_tools/kustomizations.py",
            "scripts/deploy_tools/runbook_gates_lib/k3s_upgrade.py",
            "scripts/diagnostics/probe_lib/flux.py",
            "scripts/diagnostics/probe_lib/health_alerts.py",
            "scripts/diagnostics/probe_lib/health_cronjob.py",
            "scripts/infra_map/live.py",
            "scripts/lib/longhorn_lib/longhorn_backup_health_logic.py",
            "scripts/lib/longhorn_lib/longhorn_cron_evidence_logic.py",
            "scripts/renderer/deletion_drill.py",
            "scripts/renderer/job_files/flux_soak.py",
        }
    ),
)
