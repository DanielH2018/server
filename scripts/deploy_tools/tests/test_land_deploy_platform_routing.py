"""The landing's platform filter on host routing (issues #2718, #2730).

`wg-easy` is declared twice -- a `platform: k8s` entry on daniel-box and a Compose entry on
daniel-pi -- and a k3s change reaches the tag through the k8s caller graph or through a path
under `roles/k8s/`. Routing it to both hosts added an ssh deploy of the Pi's container that the
change never touched.

`test_land_tags_landing_hosts_at.py` owns the routing rule itself and `test_land_platform.py`
owns which tags a path set proves are k3s. This owns whether `deploy_by_host` hands the subset
over, through both of its reads.

Split from test_land_deploy.py, which sits at its line cap.

Run: uv run pytest scripts/deploy_tools/tests/test_land_deploy_platform_routing.py
"""

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.exit_codes import DEPLOY_BROAD
from deploy_tools.land_lib import deploy


def _ready(landing, fakes=None, **opts):
    ln, calls = landing(fakes, **opts)
    ln.merge_sha = MERGE_SHA
    ln.ledger.t_ci = 2.0
    ln.ledger.t_tick = 3.0
    return ln, calls


def test_caller_expanded_tags_reach_the_merge_commit_read_as_k8s_only(landing):
    """Issue #2718: `wg-easy` is k8s on daniel-box and Compose on daniel-pi, and a `manifests`
    change reaches it through the k8s caller graph alone. The routing read has to be told so,
    or it adds an ssh deploy of the Pi's container that the change never touched."""
    ln, calls = _ready(landing, Fakes(hosts="", hosts_at={"daniel-box": ["wg-easy"]}))
    ln.resolved_tags = ["wg-easy"]
    ln.k8s_only = ["wg-easy"]
    assert deploy.deploy_by_host(ln, at=MERGE_SHA) == 0
    assert [c[2]["k8s_only"] for c in calls if c[0] == "landing_hosts_at"] == [
        ["wg-easy"]
    ]


def test_the_primary_fallback_carries_the_same_k8s_only_subset(landing):
    """The ref read is not the only routing path: an unreadable merge commit falls back to
    `deploy_tags.py hosts`, and fixing one arm alone leaves the extra Pi deploy there."""
    ln, calls = _ready(landing, Fakes(hosts="daniel-box\twg-easy\n", hosts_at=None))
    ln.resolved_tags = ["wg-easy"]
    ln.k8s_only = ["wg-easy"]
    assert deploy.deploy_by_host(ln, at=MERGE_SHA) == 0
    assert [c[1] for c in calls if c[0] == "deploy_tags"] == [
        ("hosts", "wg-easy", "--k8s-only=wg-easy")
    ]


def test_a_landing_that_proved_no_platform_sends_no_restriction(landing):
    """REJECTING half: the flag is absent when `classify` proved nothing, so a tag
    `expand_build_couplings` added -- or one whose paths span both role trees (#2730) -- keeps
    reaching every host that declares it."""
    ln, calls = _ready(landing, Fakes(hosts="daniel-pi\talloy\n", hosts_at=None))
    ln.resolved_tags = ["alloy"]
    assert deploy.deploy_by_host(ln, at=MERGE_SHA) == 0
    assert [c[1] for c in calls if c[0] == "deploy_tags"] == [("hosts", "alloy")]


# ── where the fallback derivation's own k8s_only comes from (issue #2738) ──

_K8S_PATH = "ansible/roles/k8s/wg-easy/templates/deployment.yaml.j2"


def _fallback(landing, fakes, **opts):
    """A landing whose tags come from the diff, `gh` having truncated the PR's file list."""
    ln, calls = _ready(landing, fakes, since="beefbeef", **opts)
    ln.needs_diff = True
    return ln, calls


def test_the_fallback_proves_a_platform_from_the_diffs_own_paths(landing):
    """CLEAN half: the range's paths all sit under the k8s tree, so the rebuilt tag routes to
    the cluster alone instead of adding an ssh deploy of the Pi's wg-easy."""
    ln, calls = _fallback(
        landing,
        Fakes(
            changed="wg-easy\n",
            diff_paths=[_K8S_PATH],
            path_k8s_only=["wg-easy"],
            hosts_at={"daniel-box": ["wg-easy"]},
        ),
    )
    deploy.derive_from_diff(ln)
    assert ln.k8s_only == ["wg-easy"]
    # Through the seam to the routing read, which is #2738's own verify-by: the attribute
    # `record_k8s_only` set has to reach `deploy_by_host`'s `landing_hosts_at` call.
    assert deploy.deploy_by_host(ln, at=MERGE_SHA) == 0
    assert [c[2]["k8s_only"] for c in calls if c[0] == "landing_hosts_at"] == [
        ["wg-easy"]
    ]
    # The DIFF's paths, not the PR's file list: the fake answers the same either way, so
    # asserting only on `k8s_only` would pass with the truncated list handed over.
    assert [c[1] for c in calls if c[0] == "k8s_only_tags"] == [(_K8S_PATH,)]
    assert [c[1] for c in calls if c[0] == "git" and c[1][0] == "diff"] == [
        ("diff", "--name-only", "beefbeef...HEAD")
    ]


def test_a_fallback_tag_named_in_both_trees_keeps_both_hosts(landing):
    """REJECTING half: the derivation proves nothing for a tag whose paths span both role
    trees, and the landing must then keep deploying the Pi (issue #929)."""
    ln, _calls = _fallback(
        landing, Fakes(changed="wg-easy\n", diff_paths=[_K8S_PATH], path_k8s_only=[])
    )
    deploy.derive_from_diff(ln)
    assert ln.k8s_only == []


def test_a_tag_the_range_proves_but_this_landing_never_deploys_drops_out(landing):
    """`--since` bounds a range wider than this PR, so the paths can prove a tag another
    session's merge contributed. Intersected with the derivation, or the stray reaches the
    `--k8s-only=` argv for a service this landing does not deploy."""
    ln, _calls = _fallback(
        landing,
        Fakes(
            changed="wg-easy\n",
            diff_paths=[_K8S_PATH],
            path_k8s_only=["sonarr", "wg-easy"],
        ),
    )
    deploy.derive_from_diff(ln)
    assert ln.k8s_only == ["wg-easy"]


def test_the_deployers_own_narrowing_proves_no_platform(landing):
    """`narrow` maps a broad path to the services whose render it reaches, and no path names
    those tags, so that branch never asks."""
    ln, calls = _fallback(
        landing,
        Fakes(
            changed_rc=DEPLOY_BROAD,
            narrowed_rc=0,
            narrowed="wg-easy\n",
            diff_paths=[_K8S_PATH],
            path_k8s_only=["wg-easy"],
        ),
    )
    deploy.derive_from_diff(ln)
    assert (ln.resolved_tags, ln.k8s_only) == (["wg-easy"], [])
    assert [c[0] for c in calls if c[0] == "k8s_only_tags"] == []


def test_a_path_read_that_fails_routes_to_both_platforms(landing):
    """Every failure falls the safe way: no restriction rather than a guessed one."""
    ln, calls = _fallback(
        landing,
        Fakes(
            changed="wg-easy\n",
            diff_rc=1,
            diff_paths=[_K8S_PATH],
            path_k8s_only=["wg-easy"],
        ),
    )
    deploy.derive_from_diff(ln)
    assert ln.k8s_only == []
    assert [c[0] for c in calls if c[0] == "k8s_only_tags"] == []
