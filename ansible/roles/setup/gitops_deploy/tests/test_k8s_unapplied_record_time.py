"""A tick drops the `k8s_unapplied` line it records when the service's record carries it (#4087).

A fast-path landing deploys at its merge commit and only then kicks the tick that merges the
range, so that tick records a change that is already live. `reconcile` ran before the record,
and the next tick is ten minutes away. The same tick's k8s deferral alert leaves a carried
service out, and skips the k8s channel when every service is carried (#6147).

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_k8s_unapplied_record_time.py
"""

from deployer_lib import deploy_k8s_owed
from deployer_lib.deploy_changes import ChangeSet
from deployer_lib.deploy_toolbox import DeployTools
from gitops_ledger import OWED_K8S_UNAPPLIED

ORIGIN = "a" * 40
APPLIED = "b" * 40


def _tools(posts=None, **kwargs) -> DeployTools:
    posts = [] if posts is None else posts
    return DeployTools(
        discord_post=lambda _webhook, content: posts.append(content) or True,
        digest_provable=lambda _repo, _roles: set(),
        **kwargs,
    )


def _record(
    state, settings, releases, is_ancestor, posts=None, k8s=("sonarr", "authelia")
):
    deploy_k8s_owed.alert_and_record_deferred(
        _tools(posts, release_commit=releases.get, is_ancestor=is_ancestor),
        state,
        settings,
        ORIGIN,
        set(),
        ChangeSet(k8s=set(k8s)),
        declared_k8s={"sonarr", "authelia"},
    )
    return sorted(e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED))


def test_a_change_a_landing_already_deployed_is_not_kept(
    gitops_deploy, settings, state
):
    """FLAGGED half (#4087): a fast-path landing deploys at its merge commit, then kicks the
    tick that records the change. sonarr's record carries ORIGIN, so its line goes at once."""
    assert _record(
        state,
        settings,
        {"sonarr": APPLIED},
        lambda _repo, origin, commit: (origin, commit) == (ORIGIN, APPLIED),
    ) == ["authelia"]


def test_a_record_predating_the_change_keeps_the_line_it_records(
    gitops_deploy, settings, state
):
    """CLEAN half: sonarr was deployed, but at a commit that does not carry ORIGIN."""
    assert _record(state, settings, {"sonarr": APPLIED}, lambda *_a: False) == [
        "authelia",
        "sonarr",
    ]


def test_the_deferral_alert_leaves_out_a_service_a_landing_already_deployed(
    gitops_deploy, settings, state
):
    """#6147: sonarr's record carries ORIGIN, so the k8s alert names authelia alone."""
    posts = []
    _record(
        state,
        settings,
        {"sonarr": APPLIED},
        lambda _repo, origin, commit: (origin, commit) == (ORIGIN, APPLIED),
        posts,
    )
    assert len(posts) == 1
    assert "authelia" in posts[0]
    assert "sonarr" not in posts[0]


def test_a_tick_after_a_landing_that_carried_every_service_posts_no_k8s_alert(
    gitops_deploy, settings, state
):
    """#6147: the landing deployed sonarr, the only change, so the tick posts nothing."""
    posts = []
    _record(
        state,
        settings,
        {"sonarr": APPLIED},
        lambda _repo, origin, commit: (origin, commit) == (ORIGIN, APPLIED),
        posts,
        k8s=("sonarr",),
    )
    assert posts == []
    assert state.alerted_sha("k8s") != ORIGIN
