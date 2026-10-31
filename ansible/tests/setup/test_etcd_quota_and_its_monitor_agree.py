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

Run: uv run pytest ansible/tests/setup/test_etcd_quota_and_its_monitor_agree.py
"""

import re

from lib import yaml_fast
from lib.repo_paths import REPO

from _helpers import K8S_ROLES

# etcd's own default, and the only value that is correct while k3s overrides nothing.
# <https://etcd.io/docs/v3.5/dev-guide/limit/#storage-size-limit>
ETCD_DEFAULT_QUOTA = 2 * 1024**3
OVERRIDE_FLAG = "quota-backend-bytes"
# The server's flags, `k3s_server_config`, since H6.4b.
K3S_SERVER_COMPONENT = REPO / "hosts" / "components" / "k3s-server" / "component.yml"
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


def k3s_overrides_the_quota() -> bool:
    """True when `k3s_server_config` passes etcd a `quota-backend-bytes` of its own."""
    return OVERRIDE_FLAG in K3S_SERVER_COMPONENT.read_text()


def test_the_threshold_matches_the_quota_actually_in_force():
    if k3s_overrides_the_quota():
        override = [
            line.strip()
            for line in K3S_SERVER_COMPONENT.read_text().splitlines()
            if OVERRIDE_FLAG in line
        ]
        assert str(rule_quota()) in " ".join(override), (
            f"k3s_server_config now sets {OVERRIDE_FLAG} ({override}) and "
            f"EtcdDBNearQuota still divides by {rule_quota()} — the rule measures "
            "against a quota the cluster does not have"
        )
        return
    assert rule_quota() == ETCD_DEFAULT_QUOTA, (
        f"k3s_server_config sets no etcd-arg {OVERRIDE_FLAG}, so etcd's own "
        f"{ETCD_DEFAULT_QUOTA}-byte default is in force, but EtcdDBNearQuota divides by "
        f"{rule_quota()}"
    )


def test_a_rule_quota_that_leaves_the_default_would_be_detected():
    """The rejecting half: the rule's quota edited while k3s overrides nothing."""
    edited = STORAGE_RULES.read_text().replace("/ 2147483648 >=", "/ 4294967296 >=")
    assert rule_quota(edited) != ETCD_DEFAULT_QUOTA


def test_a_divergent_override_would_be_detected():
    """The rejecting half, on text this repo does not carry: the flag present at another value.

    Without it the guard above is only ever observed on the no-override branch, where a `return`
    skips the comparison entirely — so a broken comparison would never be exercised.
    """
    override = "--kube-apiserver-arg=x --etcd-arg=quota-backend-bytes=8589934592"
    assert OVERRIDE_FLAG in override
    assert str(ETCD_DEFAULT_QUOTA) not in override
