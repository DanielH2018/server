"""The etcd quota the alert rule measures against and k3s's actual backend quota must name one value.

`EtcdDBNearQuota` measures `apiserver_storage_size_bytes` against a quota literal in
`files/rules/storage.yaml`, and that number is only right while etcd runs its OWN default
quota. `k3s_server_config` carries no `etcd-arg` `quota-backend-bytes`, so it does. Add one there
and the rule silently measures against a quota the cluster does not have: a threshold too low
pages on a healthy DB, one too high stays green past the point etcd goes read-only. Neither
failure is visible in a render check or in the rule's own promtool cases, because both halves are
internally consistent on their own.

Until M6 batch A5's Cutover-a2 the literal lived in monitor-bridge's Deployment env as
`ETCD_DB_QUOTA_BYTES`, and this guard read it from the render.

Run: uv run pytest hosts/tests/test_etcd_quota_and_its_monitor_agree.py
"""

import re

from lib import yaml_fast
from lib.repo_paths import K3S_SERVER

from _helpers import K8S_ROLES

# etcd's own default, and the only value that is correct while k3s overrides nothing.
# <https://etcd.io/docs/v3.5/dev-guide/limit/#storage-size-limit>
ETCD_DEFAULT_QUOTA = 2 * 1024**3
OVERRIDE_FLAG = "quota-backend-bytes"
# The server's flags, `k3s_server_config`, since H6.4b.
K3S_SERVER_COMPONENT = K3S_SERVER / "component.yml"
STORAGE_RULES = K8S_ROLES / "observability" / "files" / "rules" / "storage.yaml"
# `100 * max(apiserver_storage_size_bytes) / <quota> >= <percent>`
QUOTA_IN_EXPR = re.compile(r"/ (\d+) >= \d+\s*$")


def rule_quota(text: str | None = None) -> int:
    """The quota literal in `EtcdDBNearQuota`'s expression.

    Read off the rule rather than restated here, so a quota that moves in the rule is the number
    this guard compares.
    """
    [group] = yaml_fast.safe_load(STORAGE_RULES.read_text() if text is None else text)[
        "groups"
    ]
    [rule] = [r for r in group["rules"] if r["alert"] == "EtcdDBNearQuota"]
    match = QUOTA_IN_EXPR.search(" ".join(rule["expr"].split()))
    assert match, (
        "EtcdDBNearQuota's expression no longer divides by a plain byte quota, so nothing here "
        "pins it to the cluster"
    )
    return int(match.group(1))


def quota_problem(component: str, quota: int) -> str | None:
    """Why a rule dividing by `quota` misreads the quota `component` puts in force, else None.

    `component` is the text of `k3s_server_config`'s component file. When it passes etcd a
    `quota-backend-bytes` of its own, that line must carry `quota`; otherwise `quota` must be
    etcd's default.
    """
    override = [
        line.strip() for line in component.splitlines() if OVERRIDE_FLAG in line
    ]
    if override:
        if str(quota) in " ".join(override):
            return None
        return (
            f"k3s_server_config sets {OVERRIDE_FLAG} ({override}) and EtcdDBNearQuota "
            f"divides by {quota}: the rule measures against a quota the cluster does not have"
        )
    if quota == ETCD_DEFAULT_QUOTA:
        return None
    return (
        f"k3s_server_config sets no etcd-arg {OVERRIDE_FLAG}, so etcd's own "
        f"{ETCD_DEFAULT_QUOTA}-byte default is in force, but EtcdDBNearQuota divides by {quota}"
    )


def test_the_threshold_matches_the_quota_actually_in_force():
    assert quota_problem(K3S_SERVER_COMPONENT.read_text(), rule_quota()) is None


def test_a_rule_quota_that_leaves_the_default_is_flagged():
    """The rule's quota edited while k3s overrides nothing."""
    edited = STORAGE_RULES.read_text().replace("/ 2147483648 >=", "/ 4294967296 >=")
    assert quota_problem("", rule_quota(edited)) is not None


def test_an_override_at_another_quota_than_the_rules_is_flagged():
    component = 'etcd-arg:\n  - "quota-backend-bytes=8589934592"\n'
    assert quota_problem(component, ETCD_DEFAULT_QUOTA) is not None
    assert quota_problem(component, 8589934592) is None
