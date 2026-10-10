"""A tick drops the `k8s_unapplied` line it records when the service's record carries it (#4087).

A fast-path landing deploys at its merge commit and only then kicks the tick that merges the
range, so that tick records a change that is already live. `reconcile` ran before the record,
and the next tick is ten minutes away.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_k8s_unapplied_record_time.py
"""

import deploy_k8s_owed
from deploy_changes import ChangeSet
from deploy_toolbox import DeployTools
from gitops_ledger import OWED_K8S_UNAPPLIED

ORIGIN = "a" * 40
APPLIED = "b" * 40


def _tools(**kwargs) -> DeployTools:
    return DeployTools(
        discord_post=lambda _webhook, _content: True,
        digest_provable=lambda _repo, _roles: set(),
        **kwargs,
    )


def _record(state, settings, releases, is_ancestor):
    deploy_k8s_owed.alert_and_record_deferred(
        _tools(release_commit=releases.get, is_ancestor=is_ancestor),
        state,
        settings,
        ORIGIN,
        set(),
        ChangeSet(k8s={"sonarr", "authelia"}),
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
