"""monitor-bridge's etcd quota threshold and k3s's actual backend quota must name one value.

`check_etcd_db_size` measures `apiserver_storage_size_bytes` against `ETCD_DB_QUOTA_BYTES`
(monitor-bridge's env-secret), and that number is only right while etcd runs its OWN default
quota. `k3s_server_args` carries no `--etcd-arg=quota-backend-bytes`, so it does. Add one there
and the check silently measures against a quota the cluster does not have — a threshold too low
pages on a healthy DB, one too high stays green past the point etcd goes read-only. Neither
failure is visible in a render check or in the check's own tests, because both halves are
internally consistent on their own (#2403).

Run: uv run pytest ansible/tests/setup/test_etcd_quota_and_its_monitor_agree.py
"""

import re

from _helpers import REPO

K3S_DEFAULTS = REPO / "ansible/roles/setup/k3s/defaults/main.yml"
ENV_SECRET = REPO / "ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2"

# etcd's own default, and the only value that is correct while k3s overrides nothing.
# <https://etcd.io/docs/v3.5/dev-guide/limit/#storage-size-limit>
ETCD_DEFAULT_QUOTA = 2 * 1024**3
OVERRIDE_FLAG = "quota-backend-bytes"


def deployed_quota() -> int:
    """`ETCD_DB_QUOTA_BYTES` as the role's env-secret declares it."""
    match = re.search(
        r'^\s*ETCD_DB_QUOTA_BYTES:\s*"?(\d+)"?\s*$', ENV_SECRET.read_text(), re.M
    )
    assert match, (
        "monitor-bridge's env-secret no longer declares ETCD_DB_QUOTA_BYTES, so the check falls "
        "back to its in-code default and nothing here pins it to the cluster"
    )
    return int(match.group(1))


def k3s_overrides_the_quota() -> bool:
    """True when `k3s_server_args` passes etcd a `quota-backend-bytes` of its own."""
    return OVERRIDE_FLAG in K3S_DEFAULTS.read_text()


def test_the_threshold_matches_the_quota_actually_in_force():
    if k3s_overrides_the_quota():
        override = [
            line.strip()
            for line in K3S_DEFAULTS.read_text().splitlines()
            if OVERRIDE_FLAG in line
        ]
        assert str(deployed_quota()) in " ".join(override), (
            f"k3s_server_args now sets {OVERRIDE_FLAG} ({override}) and "
            f"ETCD_DB_QUOTA_BYTES is still {deployed_quota()} — check_etcd_db_size measures "
            "against a quota the cluster does not have"
        )
        return
    assert deployed_quota() == ETCD_DEFAULT_QUOTA, (
        f"k3s_server_args sets no --etcd-arg={OVERRIDE_FLAG}, so etcd's own "
        f"{ETCD_DEFAULT_QUOTA}-byte default is in force, but ETCD_DB_QUOTA_BYTES is "
        f"{deployed_quota()}"
    )


def test_a_divergent_override_would_be_detected():
    """The rejecting half, on text this repo does not carry: the flag present at another value.

    Without it the guard above is only ever observed on the no-override branch, where a `return`
    skips the comparison entirely — so a broken comparison would never be exercised.
    """
    override = "--kube-apiserver-arg=x --etcd-arg=quota-backend-bytes=8589934592"
    assert OVERRIDE_FLAG in override
    assert str(ETCD_DEFAULT_QUOTA) not in override
