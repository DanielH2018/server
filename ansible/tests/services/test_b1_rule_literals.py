"""The five B1 NotReporting rules carry the deadline their producer renders, as a literal.

`files/rules/host-checks.yaml` is plain YAML, so each `<Check>NotReporting` rule names its
deadline twice: the `>` threshold of the stale-file arm and the `[Ns]` window of the
absent_over_time arm. M6's batch B1 gives that pair to the five k3s crons. A cron moved to a
slower cadence without its rule would page on every healthy gap, and one moved faster would leave
the rule slower than the check (M1 convention 1). The two `*/N` crons render DEADLINE_S from their
cron minute. The three daily checks take `k3s_daily_check_deadline_s`, which the live-drift unit
passes as its `DEADLINE_S` environment variable. The disk-health pin is
test_disk_health_rule_literals.py.

Run: uv run pytest ansible/tests/services/test_b1_rule_literals.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _shell_render import render_shell_script
from lib import yaml_fast
from lib.estate import role_defaults
from lib.repo_paths import K3S_ROLE

RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
K3S = role_defaults(K3S_ROLE)
# A bare `>` comparison: `>=` and `==` are not the stale-file arm.
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")

# alert -> (producer template, cron minute variable). The variable is None for a daily check.
SHELL_CHECKS = {
    "LonghornBackupHealthNotReporting": (
        "longhorn-backup-health.sh.j2",
        "k3s_longhorn_backup_health_cron_minute",
    ),
    "ReleaseStalenessCheckNotReporting": (
        "release-staleness-check.sh.j2",
        "k3s_release_staleness_cron_minute",
    ),
    "EtcdSnapshotOffboxNotReporting": ("etcd-snapshot-offbox.sh.j2", None),
    "ManifestPruneCheckNotReporting": ("manifest-prune-check.sh.j2", None),
}
LIVE_DRIFT_ALERT = "LiveDriftCheckNotReporting"


def rule_expr(alert: str, rules: str | None = None) -> str:
    """The expression of the one rule named `alert`."""
    [group] = yaml_fast.safe_load(rules or RULES.read_text())["groups"]
    exprs = [r["expr"] for r in group["rules"] if r.get("alert") == alert]
    assert len(exprs) == 1, f"host-checks.yaml has {len(exprs)} {alert} rules"
    return exprs[0]


def rendered_deadline(template: str, overrides: dict | None = None) -> int:
    """DEADLINE_S as the producer renders it, at the role default or under `overrides`."""
    body = render_shell_script("setup", "k3s", template, overrides=overrides)
    [deadline] = re.findall(r"^DEADLINE_S=(\d+)$", body, re.M)
    return int(deadline)


def literal_problems(expr: str, deadline: int) -> list[str]:
    """Why the rule's two deadline literals differ from `deadline`, or nothing."""
    problems = []
    for name, found in (
        ("the `>` threshold", STALE_ARM.findall(expr)),
        ("the absent_over_time window", WINDOW.findall(expr)),
    ):
        if len(found) != 1:
            problems.append(f"{len(found)} of {name}, not one")
        elif int(found[0]) != deadline:
            problems.append(f"{name} reads {found[0]}s, and DEADLINE_S is {deadline}")
    return problems


@pytest.mark.parametrize("alert", sorted(SHELL_CHECKS))
def test_a_shell_producers_rule_literals_equal_its_rendered_deadline_is_clean(alert):
    template, _minute_var = SHELL_CHECKS[alert]
    assert literal_problems(rule_expr(alert), rendered_deadline(template)) == []


def test_the_live_drift_rule_literals_equal_the_deadline_its_unit_passes_is_clean():
    # live_drift_check.py is a plain file, so its deadline is the unit's DEADLINE_S variable.
    tasks = (K3S_ROLE / "tasks" / "health-crons.yml").read_text()
    assert '"DEADLINE_S={{ k3s_daily_check_deadline_s }}"' in tasks
    assert (
        literal_problems(rule_expr(LIVE_DRIFT_ALERT), K3S["k3s_daily_check_deadline_s"])
        == []
    )


def test_a_cron_that_moves_without_its_rule_is_flagged():
    # */30 renders 3900s for the heartbeat that is */10 and 1500s today, so the rule is red.
    moved = rendered_deadline(
        "longhorn-backup-health.sh.j2",
        {"k3s_longhorn_backup_health_cron_minute": "*/30"},
    )
    assert moved == 3900
    assert literal_problems(rule_expr("LonghornBackupHealthNotReporting"), moved) == [
        "the `>` threshold reads 1500s, and DEADLINE_S is 3900",
        "the absent_over_time window reads 1500s, and DEADLINE_S is 3900",
    ]


def test_a_daily_deadline_that_moves_without_its_rules_is_flagged():
    # The three daily rules read the shared default: moving it must show on every one of them.
    moved = rendered_deadline(
        "etcd-snapshot-offbox.sh.j2", {"k3s_daily_check_deadline_s": 3600}
    )
    assert moved == 3600
    assert literal_problems(rule_expr("EtcdSnapshotOffboxNotReporting"), moved)
