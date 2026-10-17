"""`chained_timeout.promote_timeout`: how long `promote.py verify` waits, as clean/flagged pairs.

Each bundle is built from the renderer's own `tag_kustomization`, as `test_verify_promotion.py`
builds its bundles, and shares that module's fixtures.

Run: uv run pytest scripts/deploy_tools/tests/test_chained_timeout.py
"""

import re

import pytest
from _promote_fakes import bundle_files
from renderer.renderer_lib.bundle import bundle_files as render_bundle
from test_verify_promotion import CHANGED, PROD, T, Clock, digests, ks, ok, reads

import chained_timeout as ct
from deploy_tools.promote_lib import verify_promotion as vp
from lib import yaml_fast
from lib.repo_paths import REPO

SYNC = REPO / "ansible/roles/k8s/flux/templates/sync.yaml.j2"

TEN_M = 10 * 60
# The OCIRepository interval and the 2m `retryInterval` the renderer writes.
TAIL = ct.OCI_INTERVAL_S + 2 * 60
HOP = ct.REQUEUE_DEPENDENCY_S


def docs(*kustomizations: str) -> list[dict]:
    return [yaml_fast.safe_load(k) for k in kustomizations]


def test_a_changed_tag_and_its_deferred_dependent_chain_two_timeouts_a_hop_and_a_round():
    """bento-pdf at the manifests role's 10m rollout timeout, with its `-deferred`."""
    assert vp.expected_set(CHANGED, PROD).timeout_s == 2 * TEN_M + HOP + TEN_M + TAIL


def test_a_three_kustomization_chain_counts_three_timeouts_two_hops_and_one_round():
    chain = docs(ks("a"), ks("b", "a"), ks("c", "b"))
    assert (
        ct.promote_timeout(chain, {"a", "b", "c"}) == 3 * TEN_M + 2 * HOP + TEN_M + TAIL
    )


def test_a_changed_shared_kind_at_the_head_of_a_chain_counts_its_timeout():
    chain = docs(
        ks("cluster-vars", timeout="5m"), ks("a", "cluster-vars"), ks("b", "a")
    )
    changed = {"cluster-vars", "a", "b"}
    assert (
        ct.promote_timeout(chain, changed)
        == 5 * 60 + 2 * TEN_M + 2 * HOP + TEN_M + TAIL
    )


def test_an_unchanged_ancestor_adds_one_hop_and_no_timeout():
    chain = docs(ks("cluster-vars", timeout="20m"), ks("a", "cluster-vars"))
    assert ct.promote_timeout(chain, {"a"}) == HOP + TEN_M + TEN_M + TAIL


def test_a_changed_ancestor_adds_its_timeout():
    chain = docs(ks("cluster-vars", timeout="20m"), ks("a", "cluster-vars"))
    found = ct.promote_timeout(chain, {"cluster-vars", "a"})
    assert found == 20 * 60 + HOP + TEN_M + 20 * 60 + TAIL


def test_eleven_changed_names_under_concurrent_five_add_three_rounds_of_the_largest():
    """An unchanged Kustomization with a larger timeout adds nothing to the term."""
    names = [f"t{i}" for i in range(11)]
    bundle = docs(
        *(ks(n) for n in names[1:]), ks("t0", timeout="15m"), ks("idle", timeout="50m")
    )
    found = ct.promote_timeout(bundle, set(names), concurrent=5)
    assert found == 15 * 60 + 3 * 15 * 60 + TAIL


def test_a_changed_soak_longer_than_the_chain_sets_the_budget():
    """`soak` has no edges, so its timeout is a term beside the chain, never added to it."""
    bundle = docs(ks("a"), ks("soak", timeout="60m"))
    assert ct.promote_timeout(bundle, {"a", "soak"}) == 60 * 60 + TAIL


def test_a_changed_soak_shorter_than_the_chain_takes_no_place_in_the_queue():
    """Five tags fill one round at `--concurrent` 5, and the soak's worker is the sixth."""
    names = [f"t{i}" for i in range(5)]
    bundle = docs(*(ks(n) for n in names), ks("soak", timeout="13m"))
    found = ct.promote_timeout(bundle, {*names, "soak"}, concurrent=5)
    assert found == TEN_M + TEN_M + TAIL


def test_a_dependent_of_a_changed_name_joins_the_queueing_term():
    """Ten changed tags fill two rounds at `--concurrent` 5; their dependent makes three."""
    names = [f"t{i}" for i in range(10)]
    plain = {n: ks(n) for n in names}
    old = bundle_files(digests(**{n: "a" for n in names}), **plain)
    new = bundle_files(digests(**{n: "b" for n in names}), **plain)
    with_dependent = bundle_files(
        digests(**{n: "b" for n in names}), **plain, last=ks("last", "t0")
    )
    for files, rounds, hops in ((new, 2, 0), (with_dependent, 3, 1)):
        expected = vp.expected_set(files, old)
        found = ct.promote_timeout(
            vp.flux_kustomizations(files), expected.names, concurrent=5
        )
        assert found == (1 + hops) * TEN_M + hops * HOP + rounds * TEN_M + TAIL


def test_a_derived_timeout_past_the_cap_gets_the_cap_and_verify_prints_failed(capsys):
    chain = docs(ks("n0"), *(ks(f"n{i}", f"n{i - 1}") for i in range(1, 40)))
    timeout = ct.promote_timeout(chain)
    assert timeout == ct.VERIFY_CAP_S == 5.5 * 3600
    expected = vp.Expected(frozenset({"homelab", "n39"}), frozenset(), "", timeout)
    clock = Clock()
    verdict = vp.verify(reads({T: ok("homelab")}, clock), T, "", expected)
    assert verdict == vp.Verdict("failed", ("n39",))
    assert vp.report(T, verdict, None) == 1
    assert capsys.readouterr().out.endswith(f"target={T} verdict=failed failed=n39\n")


def _timeout_of_render(key_sets: dict[str, frozenset[str]]) -> int:
    """The derived timeout of a bundle the renderer writes for `peanut` at a 10m timeout,
    with every Kustomization in it changed."""
    directories = {"peanut": {"kustomization.yaml": "resources: []\n"}}
    files = render_bundle(directories, key_sets, ["peanut"], "10m", "x=ENC[y]\n")
    return ct.promote_timeout(
        vp.flux_kustomizations({p: t.encode() for p, t in files.items()})
    )


def test_a_substituting_tag_chains_through_the_renderers_cluster_vars_edge():
    """cluster-vars's 10m, one hop and peanut's 10m on the path, and one round of 10m."""
    found = _timeout_of_render({"peanut": frozenset({"peanut_password"})})
    assert found == 2 * TEN_M + HOP + TEN_M + TAIL


def test_a_tag_that_does_not_substitute_has_no_edge_to_cluster_vars():
    """The negative half: two names changed, neither on a path through the other."""
    assert _timeout_of_render({"peanut": frozenset()}) == TEN_M + TEN_M + TAIL


def test_a_dependency_the_bundle_does_not_hold_is_an_error():
    """Flux would hold `peanut` at DependencyNotReady, so no wait is long enough."""
    with pytest.raises(
        ValueError, match="cluster-vars, which the bundle does not hold"
    ):
        ct.promote_timeout(docs(ks("peanut", "cluster-vars")))


def test_a_cycle_is_an_error():
    with pytest.raises(ValueError, match="cycle"):
        ct.promote_timeout(docs(ks("a", "b"), ks("b", "a")))


def test_the_requeue_interval_is_kustomize_controllers_default():
    """The vendored install and the role's flags leave it unset, so the default, 30s, holds.

    `--concurrent` is the role's flag; test_flux_role.py holds CONCURRENT to it.
    """
    flux = REPO / "ansible/roles/k8s/flux"
    assert (
        "--requeue-dependency" not in (flux / "files/gotk-components.yaml").read_text()
    )
    assert "--requeue-dependency" not in (flux / "defaults/main.yml").read_text()
    assert ct.REQUEUE_DEPENDENCY_S == 30


def test_an_empty_bundle_times_out_at_the_roots_floor():
    assert (
        ct.promote_timeout([])
        == ct.ROOT_TIMEOUT_S + ct.OCI_INTERVAL_S + ct.ROOT_RETRY_S
    )


def test_an_unreadable_duration_is_an_error():
    with pytest.raises(ValueError, match="not a duration"):
        ct.promote_timeout([{"spec": {"timeout": "ten minutes"}}])


def _sync_docs() -> list[dict]:
    """The flux role's sync template with its Jinja removed, parsed."""
    text = re.sub(r"\{#.*?#\}", "", SYNC.read_text(), flags=re.S)
    text = re.sub(r"^\s*\{%.*%\}\s*$", "", text, flags=re.M)
    text = re.sub(r"\{\{.*?\}\}", "x", text)
    return [d for d in yaml_fast.safe_load_all(text) if isinstance(d, dict)]


def test_the_floor_matches_the_flux_roles_sync_template():
    docs = {(d["kind"], d["metadata"]["name"]): d["spec"] for d in _sync_docs()}
    root = docs[("Kustomization", "homelab")]
    assert (
        ct.seconds(docs[("OCIRepository", "homelab")]["interval"]) == ct.OCI_INTERVAL_S
    )
    assert ct.seconds(root["timeout"]) == ct.ROOT_TIMEOUT_S
    assert ct.seconds(root["retryInterval"]) == ct.ROOT_RETRY_S
