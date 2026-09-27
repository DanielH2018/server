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
