"""scrutiny-web panics rather than retries when InfluxDB is unreachable, so the policy ships here.

scrutiny-web calls InfluxDB's `/api/v2/setup` during AppEngine.Setup and `panic(err)`s on a
connection error instead of retrying — upstream `webapp/backend/pkg/web/middleware/repository.go`,
read against upstream master on 2026-09-06 and recorded in `web.yaml.j2`'s `wait-for-influxdb`
comment. The init gate that absorbs it is bounded at 60 x 2s, so an unreachable InfluxDB fails the
pod rather than parking it. The NetworkPolicy granting the only path to InfluxDB lived in
`roles/k8s/netpol-baseline/` until 2026-09-10, one role away from the Deployment whose startup
depends on it: `--tags scrutiny` staged a web pod that could only time out. That is the split
#1609 closed for authelia's session store, carried forward by #1620.

`build_k8s_dep_map` cannot cover this: it derives a role's edges from templates inside that role's
own directory (`ansible/filter_plugins/toposort.py`), so a dependency expressed in a sibling role
produces no edge and no ordering constraint to violate. Co-location is what closes it, and this is
the guard that keeps it closed.

Three invariants, each a predicate with a passing and a rejecting input, then applied to the real
rendered manifests behind a non-vacuity assertion:

- **One role renders both the InfluxDB Deployment and the policy selecting it.** Roles are
  compared rather than filenames: the defect is the SPLIT, whatever the two files are called.
- **The policy's port is InfluxDB's containerPort.** A policy on the wrong port renders, applies,
  and silently drops the request scrutiny-web panics on.
- **The policy is named in `manifests_files`.** Rendering it is not shipping it — only the names
  in that list are staged and applied, so a template in the right role but absent from the list
  is the same failure with the file moved.

Run: uv run pytest ansible/tests/services/test_scrutiny_influxdb_policy.py
"""

import pytest
from _helpers import REPO
from _k8s_render import rendered_docs
from lib import yaml_fast

SCRUTINY_TASKS = REPO / "ansible/roles/k8s/scrutiny/tasks/main.yml"

BACKEND = "scrutiny-influxdb"
CONSUMER = "scrutiny-web"
INFLUXDB_PORT = 8086
POLICY_FILE = "networkpolicy-influxdb.yaml"


# --- the rules, as predicates -------------------------------------------------------


def the_policy_ships_with_the_workload(deployment_role, policy_role):
    """True when one role renders both the InfluxDB Deployment and the policy admitting web."""
    return (
        deployment_role is not None
        and policy_role is not None
        and deployment_role == policy_role
    )


def the_policy_admits_the_consumer_on_the_backend_port(policy, consumer, port):
    """True when some ingress rule admits `consumer` by pod label on exactly `port`."""
    for rule in (policy.get("spec") or {}).get("ingress") or []:
        sources = {
            ((src.get("podSelector") or {}).get("matchLabels") or {}).get("app")
            for src in rule.get("from") or []
        }
        ports = {entry.get("port") for entry in rule.get("ports") or []}
        if consumer in sources and ports == {port}:
            return True
    return False


GOOD_POLICY: dict[str, object] = {
    "spec": {
        "ingress": [
            {
                "from": [{"podSelector": {"matchLabels": {"app": CONSUMER}}}],
                "ports": [{"port": INFLUXDB_PORT, "protocol": "TCP"}],
            }
        ]
    }
}


def test_a_co_located_policy_is_clean():
    assert the_policy_ships_with_the_workload("scrutiny", "scrutiny")


def test_a_policy_in_a_sibling_role_is_flagged():
    """The literal pre-2026-09-10 state: Deployment in scrutiny, policy in netpol-baseline."""
    assert not the_policy_ships_with_the_workload("scrutiny", "netpol-baseline")


def test_a_missing_policy_is_flagged():
    """Retiring the policy without retiring InfluxDB is the same panic by another route."""
    assert not the_policy_ships_with_the_workload("scrutiny", None)


def test_a_policy_on_the_backend_port_is_clean():
    assert the_policy_admits_the_consumer_on_the_backend_port(
        GOOD_POLICY, CONSUMER, INFLUXDB_PORT
    )


def test_a_policy_on_the_wrong_port_is_flagged():
    """Renders, applies, and drops the /api/v2/setup request scrutiny-web panics on."""
    assert not the_policy_admits_the_consumer_on_the_backend_port(
        GOOD_POLICY, CONSUMER, 8087
    )


def test_a_policy_admitting_the_collector_instead_is_flagged():
    """The collector posts to scrutiny-web's API; it has no business in InfluxDB."""
    assert not the_policy_admits_the_consumer_on_the_backend_port(
        GOOD_POLICY, "scrutiny-collector", INFLUXDB_PORT
    )


# --- applied to the tree ------------------------------------------------------------


@pytest.fixture(scope="module")
def influxdb_docs():
    """The role rendering the InfluxDB Deployment, and the role plus doc of its policy."""
    found: dict[str, object] = {}
    for role, _tpl, doc in rendered_docs():
        meta = doc.get("metadata") or {}
        if doc.get("kind") == "Deployment" and meta.get("name") == BACKEND:
            found["deployment_role"] = role
            found["deployment"] = doc
        elif doc.get("kind") == "NetworkPolicy":
            selected = ((doc.get("spec") or {}).get("podSelector") or {}).get(
                "matchLabels"
            ) or {}
            if selected.get("app") == BACKEND:
                found["policy_role"] = role
                found["policy"] = doc
    assert "deployment" in found, (
        f"the render produced no {BACKEND} Deployment, so every comparison below would pass "
        f"over nothing"
    )
    return found


def test_the_influxdb_policy_ships_in_the_role_that_deploys_influxdb(influxdb_docs):
    assert the_policy_ships_with_the_workload(
        influxdb_docs["deployment_role"], influxdb_docs.get("policy_role")
    ), (
        f"the NetworkPolicy admitting {CONSUMER} to {BACKEND} must be rendered by the same role "
        f"as the {BACKEND} Deployment; got deployment in "
        f"{influxdb_docs['deployment_role']!r} and policy in "
        f"{influxdb_docs.get('policy_role')!r}. Split across roles, `--tags scrutiny` deploys a "
        f"web pod whose init gate can only time out (#1620)"
    )


def test_the_influxdb_policy_opens_the_port_the_web_pod_dials(influxdb_docs):
    container_ports = {
        exposed.get("containerPort")
        for container in influxdb_docs["deployment"]["spec"]["template"]["spec"][
            "containers"
        ]
        for exposed in container.get("ports") or []
    }
    assert INFLUXDB_PORT in container_ports, (
        f"{BACKEND}'s Deployment no longer exposes {INFLUXDB_PORT}, which web.yaml.j2 dials as "
        f"SCRUTINY_WEB_INFLUXDB_PORT; move both or neither. got {container_ports!r}"
    )
    assert the_policy_admits_the_consumer_on_the_backend_port(
        influxdb_docs["policy"], CONSUMER, INFLUXDB_PORT
    ), (
        f"{BACKEND}'s policy must admit {CONSUMER} on exactly {INFLUXDB_PORT}. A policy on "
        f"another port applies cleanly and drops the traffic"
    )


def test_the_influxdb_policy_is_staged_by_the_deploy_task():
    """Rendering it is not shipping it — `manifests_files` is what reaches the cluster."""
    tasks = yaml_fast.safe_load(SCRUTINY_TASKS.read_text())
    deploys = [
        task
        for task in tasks
        if (task.get("vars") or {}).get("manifests_service") == "scrutiny"
    ]
    assert len(deploys) == 1, (
        f"expected exactly one task deploying the scrutiny manifests, found {len(deploys)}"
    )
    files = (deploys[0].get("vars") or {}).get("manifests_files") or []
    assert POLICY_FILE in files, (
        f"{POLICY_FILE} must be named in the scrutiny role's manifests_files; got {files!r}. "
        f"Only the names in that list are staged and applied"
    )
