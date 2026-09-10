"""Authelia's sessions live in redis, and three things have to hold for that to be true.

Without a `session.redis` block Authelia's provider is IN-MEMORY — v4.39.21's own
`config.template.yml` says "Memory is the provider unless redis is defined" — so every roll of
the Deployment destroys every session, remember-me ones included. That is not visible from a
green deploy, a healthy pod or a 302: the pod is Ready either way and the redirect fires in the
forward-auth middleware before the backend is reached.

Three invariants, each a predicate with a passing and a rejecting input, then applied to the
real rendered manifests behind a non-vacuity assertion:

- **The provider is redis, addressed at the Service name.** A `session.redis` block that points
  somewhere else fails at startup rather than at render.
- **The three ports agree at 6379.** The containerPort, the Service port and the
  NetworkPolicy's port are written in three files across two roles. Any two of them agreeing is
  not enough — a policy on the wrong port renders, applies, and silently drops the traffic.
- **`authelia-redis` is not in `manifests_extra_rollouts`.** That list rolls a Deployment on
  every change to the authelia role's manifests, which is several times a day. Adding redis to
  it would destroy the sessions this whole change exists to keep, and would read as a tidy-up.
"""

import pytest
from _helpers import REPO
from _k8s_render import rendered_docs
from lib import yaml_fast

REDIS_PORT = 6379
REDIS_SERVICE = "authelia-redis"

AUTHELIA_TASKS = REPO / "ansible/roles/k8s/authelia/tasks/main.yml"


# --- the rules, as predicates -------------------------------------------------------


def sessions_are_stored_in_redis(session):
    """True when the session provider is redis and points at the in-cluster Service."""
    redis = session.get("redis") or {}
    return redis.get("host") == REDIS_SERVICE and redis.get("port") == REDIS_PORT


def the_session_store_is_not_rolled_with_the_portal(extra_rollouts):
    """True when no entry rolls the redis Deployment.

    `manifests_extra_rollouts` is keyed on a workload NAME, so this reads the names rather than
    the whole entry — an entry may also carry an `image`.
    """
    return not any(
        (entry or {}).get("name") == REDIS_SERVICE for entry in extra_rollouts or []
    )


GOOD_SESSION: dict[str, object] = {
    "secret": "x",
    "redis": {"host": REDIS_SERVICE, "port": REDIS_PORT, "database_index": 0},
}


def test_a_redis_backed_session_is_clean():
    assert sessions_are_stored_in_redis(GOOD_SESSION)


def test_a_session_with_no_redis_block_is_flagged():
    """The state this whole change exists to leave: the provider silently falls back to memory."""
    assert not sessions_are_stored_in_redis({"secret": "x"})


def test_a_redis_block_on_the_wrong_port_is_flagged():
    assert not sessions_are_stored_in_redis(
        {**GOOD_SESSION, "redis": {"host": REDIS_SERVICE, "port": 6380}}
    )


def test_no_extra_rollouts_is_clean():
    assert the_session_store_is_not_rolled_with_the_portal([])
    assert the_session_store_is_not_rolled_with_the_portal(
        [{"name": "authelia-something-else", "image": "x"}]
    )


def test_rolling_redis_with_the_portal_is_flagged():
    assert not the_session_store_is_not_rolled_with_the_portal(
        [{"name": REDIS_SERVICE, "image": "redis:7.4-alpine"}]
    )


# --- applied to the real tree --------------------------------------------------------


@pytest.fixture(scope="module")
def authelia_session():
    for role, _tpl, doc in rendered_docs():
        if role != "authelia" or doc.get("kind") != "Secret":
            continue
        if (doc.get("metadata") or {}).get("name") != "authelia-config":
            continue
        raw = (doc.get("stringData") or {}).get("configuration.yml")
        session = (yaml_fast.safe_load(raw or "") or {}).get("session")
        assert session, (
            "the rendered Authelia config carries no session block, so the rule below would "
            "assert over an empty mapping"
        )
        return session
    pytest.fail("no rendered authelia-config Secret")


@pytest.fixture(scope="module")
def redis_ports():
    """The containerPort, Service port and NetworkPolicy port, from the rendered manifests."""
    found: dict[str, int] = {}
    for _role, _tpl, doc in rendered_docs():
        name = (doc.get("metadata") or {}).get("name")
        if name != REDIS_SERVICE:
            continue
        kind = doc.get("kind")
        if kind == "Deployment":
            containers = doc["spec"]["template"]["spec"]["containers"]
            found["container"] = containers[0]["ports"][0]["containerPort"]
        elif kind == "Service":
            found["service"] = doc["spec"]["ports"][0]["port"]
        elif kind == "NetworkPolicy":
            rules = doc["spec"]["ingress"][0]
            found["networkpolicy"] = rules["ports"][0]["port"]
    missing = {"container", "service", "networkpolicy"} - set(found)
    assert not missing, (
        f"the render produced no {sorted(missing)} for {REDIS_SERVICE}, so a port comparison "
        f"below would pass over an empty set"
    )
    return found


def test_the_rendered_session_provider_is_redis(authelia_session):
    assert sessions_are_stored_in_redis(authelia_session), (
        f"Authelia's session provider must be the in-cluster redis; got "
        f"{authelia_session.get('redis')!r}. With no redis block the provider is in-memory and "
        f"every rollout logs the fleet out"
    )


def test_the_rendered_redis_ports_agree(redis_ports):
    assert set(redis_ports.values()) == {REDIS_PORT}, (
        f"the containerPort, Service port and NetworkPolicy port must all be {REDIS_PORT}; got "
        f"{redis_ports!r}. A policy on the wrong port applies cleanly and drops the traffic"
    )


def test_the_role_does_not_roll_redis_with_the_portal():
    tasks = yaml_fast.safe_load(AUTHELIA_TASKS.read_text())
    deploys = [
        task
        for task in tasks
        if (task.get("vars") or {}).get("manifests_service") == "authelia"
    ]
    assert len(deploys) == 1, (
        f"expected exactly one task deploying the authelia manifests, found {len(deploys)} — "
        f"this guard reads that task's vars and would pass vacuously over none"
    )
    extra = (deploys[0].get("vars") or {}).get("manifests_extra_rollouts")
    assert the_session_store_is_not_rolled_with_the_portal(extra), (
        f"{REDIS_SERVICE} must not be in manifests_extra_rollouts; got {extra!r}. That list "
        f"rolls on every manifest change to this role, which would destroy the sessions redis "
        f"is here to keep"
    )


# --- the policy ships with the workload it lets start (#1609) -------------------------
#
# A fourth invariant, added after the 2026-09-10 outage. The three above all hold while the
# NetworkPolicy sits in a DIFFERENT role: `rendered_docs()` sweeps the whole tree, so the port
# comparison finds the policy wherever it lives. That is exactly the state that took SSO down —
# the policy shipped in netpol-baseline, `--tags authelia` staged a portal with no path to its
# session store, and Authelia exits rather than degrades when the session provider fails its
# startup check.
#
# `build_k8s_dep_map` cannot cover this: it derives a role's edges from templates inside that
# role's own directory (ansible/filter_plugins/toposort.py), so a dependency expressed in a
# sibling role produces no edge and no ordering constraint to violate. Co-location is what
# closes it, and this is the guard that keeps it closed.

REDIS_POLICY_FILE = "networkpolicy-redis.yaml"


def the_policy_ships_with_the_workload(deployment_role, policy_role):
    """True when one role renders both the redis Deployment and the policy admitting authelia.

    Roles are compared rather than filenames: the defect is the SPLIT, and it is a split
    whatever the two files are called.
    """
    return (
        deployment_role is not None
        and policy_role is not None
        and deployment_role == policy_role
    )


def test_a_co_located_policy_is_clean():
    assert the_policy_ships_with_the_workload("authelia", "authelia")


def test_a_policy_in_a_sibling_role_is_flagged():
    """The literal 2026-09-10 state: the Deployment in authelia, the policy in netpol-baseline."""
    assert not the_policy_ships_with_the_workload("authelia", "netpol-baseline")


def test_a_missing_policy_is_flagged():
    """Retiring the policy without retiring redis is the same outage by a different route."""
    assert not the_policy_ships_with_the_workload("authelia", None)


@pytest.fixture(scope="module")
def redis_doc_roles():
    """Which role renders the redis Deployment, and which renders the policy selecting it."""
    roles: dict[str, str] = {}
    for role, _tpl, doc in rendered_docs():
        meta = doc.get("metadata") or {}
        if doc.get("kind") == "Deployment" and meta.get("name") == REDIS_SERVICE:
            roles["deployment"] = role
        elif doc.get("kind") == "NetworkPolicy":
            selector = ((doc.get("spec") or {}).get("podSelector") or {}).get(
                "matchLabels"
            ) or {}
            if selector.get("app") == REDIS_SERVICE:
                roles["policy"] = role
    assert "deployment" in roles, (
        f"the render produced no {REDIS_SERVICE} Deployment, so the comparison below would "
        f"pass over nothing"
    )
    return roles


def test_the_redis_policy_ships_in_the_role_that_deploys_redis(redis_doc_roles):
    assert the_policy_ships_with_the_workload(
        redis_doc_roles["deployment"], redis_doc_roles.get("policy")
    ), (
        f"the NetworkPolicy admitting authelia to {REDIS_SERVICE} must be rendered by the same "
        f"role as the {REDIS_SERVICE} Deployment; got deployment in "
        f"{redis_doc_roles['deployment']!r} and policy in {redis_doc_roles.get('policy')!r}. "
        f"Split across roles, `--tags authelia` deploys a portal that cannot start (#1609)"
    )


def test_the_redis_policy_is_staged_by_the_deploy_task():
    """Rendering it is not shipping it — `manifests_files` is what reaches the cluster.

    A template present in the role but absent from this list is staged by nothing, which is
    the same outage with the file in the right place.
    """
    tasks = yaml_fast.safe_load(AUTHELIA_TASKS.read_text())
    deploys = [
        task
        for task in tasks
        if (task.get("vars") or {}).get("manifests_service") == "authelia"
    ]
    assert len(deploys) == 1, (
        f"expected exactly one task deploying the authelia manifests, found {len(deploys)}"
    )
    files = str((deploys[0].get("vars") or {}).get("manifests_files"))
    assert REDIS_POLICY_FILE in files, (
        f"{REDIS_POLICY_FILE} must be named in the authelia role's manifests_files; got "
        f"{files!r}. Only the names in that list are staged and applied"
    )
