"""The condition-status census row: `scripts/` compares a condition's status as an enum member.

The row sits in its own module because `test_census_rows_python.py`, which imports it into
`ROWS`, is at the cap for a test module.
"""

import re

from _row_table import Census, Subject, lines_matching, tracked

CONDITION_STATUS_IS_PARSED = Census(
    name="scripts-read-condition-status-through-the-shared-parser",
    reason=(
        "A condition's status is `True`, `False` or `Unknown`, and a hand-written compare "
        "against the string reads a misspelt literal as a condition that never holds. "
        "`lib.k8s_conditions.parse_conditions` reads `.status.conditions` into a "
        "`ConditionStatus`; compare with `is ConditionStatus.TRUE`. Every module under "
        "`scripts/` outside the tests is in scope, the soak Job's `flux_soak.py` and the "
        "host-shipped `longhorn_lib` included."
    ),
    files=lambda: [rel for rel in tracked("scripts/*.py") if "/tests/" not in rel],
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
