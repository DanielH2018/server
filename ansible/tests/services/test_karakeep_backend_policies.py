"""karakeep cannot leave Init without the two NetworkPolicies admitting it to its backends.

`deployment.yaml.j2`'s `wait-for-deps` initContainer retries `karakeep-chrome:9222` and
`karakeep-meilisearch:7700` in an UNBOUNDED loop. karakeep the app degrades when meilisearch is
unreachable — it logs search errors and serves — but the pod never reaches the app: with either
policy absent it sits in Init forever, with no restart count to read and no CrashLoopBackOff to
notice. Both policies lived in `roles/k8s/netpol-baseline/` until 2026-09-10, one role away from
the Deployment they let start, which is the split that took SSO down for 25 minutes when it was
authelia's session store (#1609) and is carried forward for these two by #1620.

`build_k8s_dep_map` cannot cover this: it derives a role's edges from templates inside that
role's own directory (`ansible/filter_plugins/toposort.py`), so a dependency expressed in a
sibling role produces no edge and no ordering constraint to violate. Co-location is what closes
it, and this is the guard that keeps it closed.

Three invariants, each a predicate with a passing and a rejecting input, then applied to the real
rendered manifests behind a non-vacuity assertion:

- **One role renders both the backend Deployment and the policy selecting it.** Roles are
  compared rather than filenames: the defect is the SPLIT, whatever the two files are called.
- **The policy's port is the port the init container dials.** A policy on the wrong port renders,
  applies, and silently drops the traffic karakeep is blocked on.
- **The policy is named in `manifests_files`.** Rendering it is not shipping it — only the names
  in that list are staged and applied, so a template in the right role but absent from the list
  is the same stall with the file moved.

Run: uv run pytest ansible/tests/services/test_karakeep_backend_policies.py
"""

import pytest
from _helpers import REPO
from _k8s_render import rendered_docs
from lib import yaml_fast

KARAKEEP_TASKS = REPO / "ansible/roles/k8s/karakeep/tasks/main.yml"

# backend workload -> (the policy file that must be staged, the port wait-for-deps dials)
BACKENDS = {
    "karakeep-chrome": ("networkpolicy-chrome.yaml", 9222),
    "karakeep-meilisearch": ("networkpolicy-meilisearch.yaml", 7700),
}
CONSUMER = "karakeep"


# --- the rules, as predicates -------------------------------------------------------


def the_policy_ships_with_the_workload(deployment_role, policy_role):
    """True when one role renders both the backend Deployment and the policy admitting karakeep."""
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
                "ports": [{"port": 7700, "protocol": "TCP"}],
            }
        ]
    }
}


def test_a_co_located_policy_is_clean():
    assert the_policy_ships_with_the_workload("karakeep", "karakeep")


def test_a_policy_in_a_sibling_role_is_flagged():
    """The literal pre-2026-09-10 state: Deployment in karakeep, policy in netpol-baseline."""
    assert not the_policy_ships_with_the_workload("karakeep", "netpol-baseline")


def test_a_missing_policy_is_flagged():
    """Retiring the policy without retiring the backend is the same stall by another route."""
    assert not the_policy_ships_with_the_workload("karakeep", None)


def test_a_policy_on_the_backend_port_is_clean():
    assert the_policy_admits_the_consumer_on_the_backend_port(
        GOOD_POLICY, CONSUMER, 7700
    )


def test_a_policy_on_the_wrong_port_is_flagged():
    """Renders, applies, and drops the traffic wait-for-deps is blocked on."""
    assert not the_policy_admits_the_consumer_on_the_backend_port(
        GOOD_POLICY, CONSUMER, 7701
    )


def test_a_policy_admitting_someone_else_is_flagged():
    assert not the_policy_admits_the_consumer_on_the_backend_port(
        GOOD_POLICY, "karakeep-time-tagger", 7700
    )


# --- applied to the tree ------------------------------------------------------------


@pytest.fixture(scope="module")
def backend_docs():
    """Per backend: the role rendering its Deployment, and the role plus doc of its policy."""
    found: dict[str, dict] = {name: {} for name in BACKENDS}
    for role, _tpl, doc in rendered_docs():
        meta = doc.get("metadata") or {}
        name = meta.get("name")
        if doc.get("kind") == "Deployment" and name in found:
            found[name]["deployment_role"] = role
            found[name]["deployment"] = doc
        elif doc.get("kind") == "NetworkPolicy":
            selected = ((doc.get("spec") or {}).get("podSelector") or {}).get(
                "matchLabels"
            ) or {}
            if selected.get("app") in found:
                found[selected["app"]]["policy_role"] = role
                found[selected["app"]]["policy"] = doc
    for name in BACKENDS:
        assert "deployment" in found[name], (
            f"the render produced no {name} Deployment, so every comparison below would pass "
            f"over nothing"
        )
    return found


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_each_backend_policy_ships_in_the_role_that_deploys_the_backend(
    backend, backend_docs
):
    entry = backend_docs[backend]
    assert the_policy_ships_with_the_workload(
        entry["deployment_role"], entry.get("policy_role")
    ), (
        f"the NetworkPolicy admitting {CONSUMER} to {backend} must be rendered by the same role "
        f"as the {backend} Deployment; got deployment in {entry['deployment_role']!r} and policy "
        f"in {entry.get('policy_role')!r}. Split across roles, `--tags karakeep` deploys a pod "
        f"that waits in Init forever (#1620)"
    )


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_each_backend_policy_opens_the_port_the_init_container_dials(
    backend, backend_docs
):
    _policy_file, port = BACKENDS[backend]
    entry = backend_docs[backend]
    container_ports = {
        exposed.get("containerPort")
        for container in entry["deployment"]["spec"]["template"]["spec"]["containers"]
        for exposed in container.get("ports") or []
    }
    assert port in container_ports, (
        f"{backend}'s Deployment no longer exposes {port}, which wait-for-deps in "
        f"deployment.yaml.j2 dials; move both or neither. got {container_ports!r}"
    )
    assert the_policy_admits_the_consumer_on_the_backend_port(
        entry["policy"], CONSUMER, port
    ), (
        f"{backend}'s policy must admit {CONSUMER} on exactly {port}, the port wait-for-deps "
        f"dials. A policy on another port applies cleanly and drops the traffic"
    )


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_each_backend_policy_is_staged_by_the_deploy_task(backend):
    """Rendering it is not shipping it — `manifests_files` is what reaches the cluster."""
    policy_file, _port = BACKENDS[backend]
    tasks = yaml_fast.safe_load(KARAKEEP_TASKS.read_text())
    deploys = [
        task
        for task in tasks
        if (task.get("vars") or {}).get("manifests_service") == CONSUMER
    ]
    assert len(deploys) == 1, (
        f"expected exactly one task deploying the karakeep manifests, found {len(deploys)}"
    )
    files = (deploys[0].get("vars") or {}).get("manifests_files") or []
    assert policy_file in files, (
        f"{policy_file} must be named in the karakeep role's manifests_files; got {files!r}. "
        f"Only the names in that list are staged and applied"
    )
