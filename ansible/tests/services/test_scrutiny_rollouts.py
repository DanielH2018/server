"""scrutiny's InfluxDB and collector rollouts are waited on and never restarted by the role.

scrutiny's primary rollout is `scrutiny-web`. A digest bump of `scrutiny_k8s_influxdb_image` or
`scrutiny_k8s_collector_image` rolls the other two workloads through the apply, and until they
were listed in `manifests_extra_rollouts` nothing waited on that roll, the karakeep-chrome
defect #4564 fixed. Both carry `restart_on: []`: the collector re-reads a kubelet-synced
ConfigMap on each cron run, and InfluxDB reads its credentials only at first-run setup, so a
restart on every scrutiny manifest change would only take InfluxDB down.

Rendered through Ansible's own filters against scrutiny's real include vars, with the `kind`
and `image` the play's `| default(...)` gives an entry that omits them.

Run: uv run pytest ansible/tests/services/test_scrutiny_rollouts.py
"""

from _helpers import ANSIBLE
from _release_expectation import extra_restarts, include_role_vars, queued_extras

SCRUTINY = ANSIBLE / "roles/k8s/scrutiny"
EXPECTED = {"scrutiny-influxdb": "deploy", "scrutiny-collector": "daemonset"}

# An image bump of both: every manifest signal fires, and the apply moved both templates.
_BUMP = dict(
    manifests_service="scrutiny",
    manifests_rollout="scrutiny-web",
    manifests_apply={
        "changed": True,
        "stdout": "deployment.apps/scrutiny-influxdb configured\n"
        "daemonset.apps/scrutiny-collector configured",
    },
)


def _extras() -> list[dict]:
    return [
        {"kind": "deploy", "image": entry["name"], **entry}
        for entry in include_role_vars(SCRUTINY)["manifests_extra_rollouts"]
    ]


def test_the_drain_waits_on_influxdb_and_the_collector_and_restarts_neither():
    queued = {r["name"]: r["kind"] for r in queued_extras(_extras(), **_BUMP)}
    assert queued == EXPECTED, (
        "the drain must wait on both non-primary scrutiny workloads, each with the kind "
        "`rollout status` needs"
    )
    restarted = {item["name"]: extra_restarts(item, **_BUMP) for item in _extras()}
    assert restarted == dict.fromkeys(EXPECTED, False)
