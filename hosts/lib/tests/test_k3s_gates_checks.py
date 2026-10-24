"""Each cluster check of `k3s_gates` as a refused/clean pair.

A check that refuses on a read error, a missing member or an empty list is only believed once a
fixture that differs in that one place passes. Every `REFUSED` row names the single change from
`healthy()` that must refuse, and `CLEAN` names the near-identical document that must pass.
"""

import pytest
from _k3s_gates_fixtures import (
    MUNGED,
    NOW,
    SOURCE_PATH,
    VERSION,
    backup,
    coredns,
    fake_get,
    healthy,
    job,
    kustomization,
    lease,
    listing,
    node,
    plan,
    volume,
    with_routes,
)

import k3s_gates
from k3s_gates import ReadError

SERVER_PLAN = f"{k3s_gates.PLANS_PATH}/{k3s_gates.SERVER_PLAN}"
AGENT_PLAN = f"{k3s_gates.PLANS_PATH}/{k3s_gates.AGENT_PLAN}"
BOTH = (node("daniel-box", server=True), node("daniel-server"))


def _run(check, routes, *args):
    """The refusals the runner would print: a raised read error counts as one."""
    get = fake_get(routes)
    extra = ([],) if check is k3s_gates.check_flux_settled else args
    return k3s_gates.run_round([(check.__name__, lambda: check(get, *extra))])


# (id, check, routes, extra args). Each row differs from healthy() in one document.
REFUSED = [
    ("volume-degraded", k3s_gates.check_volumes, with_routes(volumes=listing(volume(), volume("b", "degraded"))), ()),
    ("volume-faulted", k3s_gates.check_volumes, with_routes(volumes=listing(volume("b", "faulted"))), ()),
    ("volume-unrecognised", k3s_gates.check_volumes, with_routes(volumes=listing(volume("b", "renamed"))), ()),
    ("volume-no-robustness", k3s_gates.check_volumes, with_routes(volumes=listing(volume("b", None))), ()),
    ("volume-list-empty", k3s_gates.check_volumes, with_routes(volumes=listing()), ()),
    ("volume-list-without-items", k3s_gates.check_volumes, with_routes(volumes={}), ()),
    ("backup-in-progress", k3s_gates.check_backups, with_routes(backups=listing(backup(), backup("b", "InProgress"))), ()),
    ("backup-no-state-yet", k3s_gates.check_backups, with_routes(backups=listing(backup("b", None))), ()),
    ("node-not-ready", k3s_gates.check_nodes, with_routes(nodes=listing(node("daniel-box"), node("daniel-server", "False"))), ()),
    ("node-missing", k3s_gates.check_nodes, with_routes(nodes=listing(node("daniel-box"))), ()),
    ("node-list-empty", k3s_gates.check_nodes, with_routes(nodes=listing()), ()),
    ("coredns-rolling", k3s_gates.check_coredns, with_routes(coredns=coredns(updatedReplicas=1)), ()),
    ("coredns-surge", k3s_gates.check_coredns, with_routes(coredns=coredns(replicas=3)), ()),
    ("coredns-generation-behind", k3s_gates.check_coredns, with_routes(coredns=coredns(observedGeneration=3)), ()),
    ("coredns-field-missing", k3s_gates.check_coredns, with_routes(coredns={"metadata": {"generation": 4}, "status": {}}), ()),
    ("coredns-no-replica", k3s_gates.check_coredns, with_routes(coredns=coredns(replicas=0, updatedReplicas=0, availableReplicas=0)), ()),
    ("lease-acquired-5-minutes-ago", k3s_gates.check_server_restart, with_routes(lease=lease(acquired_s=300)), (NOW,)),
    ("lease-acquired-in-the-future", k3s_gates.check_server_restart, with_routes(lease=lease(acquired_s=-60)), (NOW,)),
    ("lease-no-holder", k3s_gates.check_server_restart, with_routes(lease=lease(holder=None)), (NOW,)),
    ("lease-empty-holder", k3s_gates.check_server_restart, with_routes(lease=lease(holder="")), (NOW,)),
    ("lease-renew-gap", k3s_gates.check_server_restart, with_routes(lease=lease(renewed_s=46)), (NOW,)),
    ("lease-no-spec", k3s_gates.check_server_restart, with_routes(lease={}), (NOW,)),
    ("agent-server-plan-applying", k3s_gates.check_server_plan_done, {**healthy(), SERVER_PLAN: plan("s", applying=["daniel-box"])}, (VERSION,)),
    ("agent-server-plan-not-complete", k3s_gates.check_server_plan_done, {**healthy(), SERVER_PLAN: plan("s", complete="False")}, (VERSION,)),
    ("agent-server-plan-stale-version", k3s_gates.check_server_plan_done, {**healthy(), SERVER_PLAN: plan("s", latest="v1.37.1-k3s1")}, (VERSION,)),
    ("agent-server-node-behind", k3s_gates.check_server_plan_done, with_routes(nodes=listing(node("daniel-box", version="v1.37.1+k3s1", server=True))), (VERSION,)),
    ("agent-no-control-plane-node", k3s_gates.check_server_plan_done, with_routes(nodes=listing(node("daniel-server"))), (VERSION,)),
    ("agent-version-unset", k3s_gates.check_server_plan_done, healthy(), (None,)),
    ("suc-plan-applying", k3s_gates.check_suc_idle, {**healthy(), AGENT_PLAN: plan("a", applying=["daniel-server"])}, ()),
    ("suc-job-running", k3s_gates.check_suc_idle, with_routes(jobs=listing(job("a"), job("b", None))), ()),
    ("flux-not-ready", k3s_gates.check_flux_settled, with_routes(kustomizations=listing(kustomization("homelab", status={"observedGeneration": 3, "lastAppliedRevision": "prod@sha256:aaa", "conditions": [{"type": "Ready", "status": "False"}]}))), ()),
    ("flux-generation-behind", k3s_gates.check_flux_settled, with_routes(kustomizations=listing(kustomization("homelab", metadata={"name": "homelab", "namespace": "flux-system", "generation": 4}))), ()),
    ("flux-behind-its-source", k3s_gates.check_flux_settled, {**healthy(), SOURCE_PATH: {"status": {"artifact": {"revision": "prod@sha256:bbb"}}}}, ()),
    ("flux-source-has-no-artifact", k3s_gates.check_flux_settled, {**healthy(), SOURCE_PATH: {"status": {}}}, ()),
    ("flux-list-empty", k3s_gates.check_flux_settled, with_routes(kustomizations=listing()), ()),
    ("flux-root-missing", k3s_gates.check_flux_settled, with_routes(kustomizations=listing(kustomization("other"))), ()),
    ("flux-unknown-source-kind", k3s_gates.check_flux_settled, with_routes(kustomizations=listing(kustomization("homelab", spec={"sourceRef": {"kind": "Bucket", "name": "x"}}))), ()),
]  # fmt: skip

CLEAN = [
    ("volume-unknown-is-idle", k3s_gates.check_volumes, with_routes(volumes=listing(volume(), volume("b", "unknown"))), ()),
    ("backup-all-settled", k3s_gates.check_backups, with_routes(backups=listing(backup(), backup("b", "Error"), backup("c", "Unknown"))), ()),
    ("backup-list-empty", k3s_gates.check_backups, with_routes(backups=listing()), ()),
    ("nodes-both-ready", k3s_gates.check_nodes, healthy(), ()),
    ("coredns-finished", k3s_gates.check_coredns, healthy(), ()),
    ("lease-acquired-20-minutes-renewed-5-seconds", k3s_gates.check_server_restart, with_routes(lease=lease(acquired_s=1200, renewed_s=5)), (NOW,)),
    ("lease-renewed-within-duration-plus-slack", k3s_gates.check_server_restart, with_routes(lease=lease(renewed_s=44)), (NOW,)),
    ("agent-server-plan-done", k3s_gates.check_server_plan_done, healthy(), (VERSION,)),
    ("agent-version-in-munged-form", k3s_gates.check_server_plan_done, healthy(), (MUNGED,)),
    ("suc-idle", k3s_gates.check_suc_idle, healthy(), ()),
    ("suc-no-jobs", k3s_gates.check_suc_idle, with_routes(jobs=listing()), ()),
    ("suc-failed-job-is-finished", k3s_gates.check_suc_idle, with_routes(jobs=listing(job("a", "Failed"))), ()),
    ("flux-settled", k3s_gates.check_flux_settled, healthy(), ()),
]  # fmt: skip


@pytest.mark.parametrize(
    "check, routes, args", [r[1:] for r in REFUSED], ids=[r[0] for r in REFUSED]
)
def test_check_is_refused(check, routes, args):
    assert _run(check, routes, *args), "the document should have been refused"


@pytest.mark.parametrize(
    "check, routes, args", [r[1:] for r in CLEAN], ids=[r[0] for r in CLEAN]
)
def test_check_is_clean(check, routes, args):
    assert _run(check, routes, *args) == []


# A read error is not a refusal list: the runner turns it into one. Every check must raise
# ReadError (never return []) when its document cannot be read.
READ_PATHS = {
    k3s_gates.check_volumes: k3s_gates.VOLUMES_PATH,
    k3s_gates.check_backups: k3s_gates.BACKUPS_PATH,
    k3s_gates.check_nodes: k3s_gates.NODES_PATH,
    k3s_gates.check_coredns: k3s_gates.COREDNS_PATH,
    k3s_gates.check_suc_idle: SERVER_PLAN,
    k3s_gates.check_flux_settled: k3s_gates.KUSTOMIZATIONS_PATH,
}


@pytest.mark.parametrize("check", READ_PATHS, ids=lambda c: c.__name__)
def test_a_read_error_is_refused(check):
    routes = {**healthy(), READ_PATHS[check]: ReadError("HTTP 403")}
    args = [[]] if check is k3s_gates.check_flux_settled else []
    with pytest.raises(ReadError):
        check(fake_get(routes), *args)


@pytest.mark.parametrize("check", READ_PATHS, ids=lambda c: c.__name__)
def test_a_healthy_read_is_not_refused(check):
    args = [[]] if check is k3s_gates.check_flux_settled else []
    assert check(fake_get(healthy()), *args) == []


def test_a_forbidden_lease_is_refused():
    routes = {**healthy(), k3s_gates.LEASE_PATH: ReadError("HTTP 403")}
    with pytest.raises(ReadError):
        k3s_gates.check_server_restart(fake_get(routes), NOW)


def test_the_server_restart_red_fixture_ignores_a_months_old_ready_transition():
    """A Lease acquired 5 minutes ago refuses beside a `Ready` that last moved months ago."""
    old_ready = node("daniel-box", server=True)
    old_ready["status"]["conditions"][0]["lastTransitionTime"] = "2026-08-01T23:41:59Z"
    routes = with_routes(
        nodes=listing(old_ready, node("daniel-server")), lease=lease(acquired_s=300)
    )
    get = fake_get(routes)
    assert k3s_gates.check_nodes(get) == []
    assert "restarted 300s ago" in k3s_gates.check_server_restart(get, NOW)[0]


def test_a_suspended_kustomization_is_skipped_and_named():
    stuck = kustomization(
        "slow",
        spec={
            "suspend": True,
            "sourceRef": {"kind": "OCIRepository", "name": "homelab"},
        },
    )
    stuck["status"] = {"observedGeneration": 1, "conditions": []}
    notes: list[str] = []
    routes = with_routes(kustomizations=listing(kustomization("homelab"), stuck))
    assert k3s_gates.check_flux_settled(fake_get(routes), notes) == []
    assert notes == ["Kustomization slow is suspended: skipped"]


def test_a_suspended_kustomization_does_not_excuse_a_missing_root():
    stuck = kustomization("other", spec={"suspend": True})
    routes = with_routes(kustomizations=listing(stuck))
    assert k3s_gates.check_flux_settled(fake_get(routes), []) != []


def test_run_round_turns_a_read_error_and_a_malformed_document_into_refusals():
    routes = {
        **healthy(),
        k3s_gates.VOLUMES_PATH: ReadError("HTTP 403"),
        k3s_gates.NODES_PATH: {"items": "oops"},
    }
    get = fake_get(routes)
    checks: list[k3s_gates.Check] = [
        ("volumes", lambda: k3s_gates.check_volumes(get)),
        ("nodes", lambda: k3s_gates.check_nodes(get)),
        ("coredns", lambda: k3s_gates.check_coredns(get)),
    ]
    found = k3s_gates.run_round(checks)
    assert len(found) == 2
    assert found[0].startswith("volumes: cannot decide") and found[1].startswith(
        "nodes: cannot decide"
    )
