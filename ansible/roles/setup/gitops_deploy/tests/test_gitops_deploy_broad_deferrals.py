#!/usr/bin/env python3
"""What a broad tick SAYS about the half of its range it did not deploy.

The sibling suite (`test_gitops_deploy_broad_k8s.py`) covers what a broad range deploys. This
one covers the pages it sends about everything else, and the three ways that went wrong: a
rotation's page dropped by a failed apply (#2383), a k8s role called unapplied that the tick's
own deploy plane had just applied (#2453), and the budget-deferred bump whose only signal is
the post those two changes must not take away (#2449).

Each rule is a pair, for the reason the sibling suite states: a channel that fired on every
range and one that fired on none read the same from the accepting side alone.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_deferrals.py
"""

import dataclasses

import deploy_locks
from _broad_k8s_range import (
    APPLYABLE_ROLE,
    DECLARES_SONARR,
    DEPLOY_PLANE,
    DEPLOY_PLANE_FULL,
    DEPLOY_SONARR,
    HAND_EDITED_K8S,
    LOCAL,
    ORIGIN,
    SECRETS,
    marker,
    mixed,
)


def test_a_failed_broad_apply_still_pages_a_secret_riding_the_same_range(
    gitops_deploy, tick, settings, state_dir
):
    """#2383: every arm that leaves the range merged sends the page itself.

    `alert_once` advances its marker on DETECTION and the range has already fast-forwarded, so
    `local == origin` and every later tick noops. A page skipped here is never sent, and the
    rotated value sits merged and stale in every consumer with nothing naming it.
    """
    config = mixed(settings, tick, APPLYABLE_ROLE, SECRETS)
    tick.playbook_outcomes = [RuntimeError("the setup plane blew up")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert marker(state_dir, "secrets_alerted_sha") == ORIGIN
    assert any("`secrets.yml` changed" in post for post in tick.posts)


def test_a_failed_broad_apply_pages_no_secret_the_range_never_carried(
    gitops_deploy, tick, settings, state_dir
):
    """The rejecting half: firing on the failure arm must not mean firing unconditionally."""
    config = mixed(settings, tick, APPLYABLE_ROLE)
    tick.playbook_outcomes = [RuntimeError("the setup plane blew up")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert marker(state_dir, "secrets_alerted_sha") is None
    assert not [post for post in tick.posts if "`secrets.yml` changed" in post]


def test_a_contended_tick_pages_no_secret_until_the_retry_merges(
    gitops_deploy, tick, settings, state_dir
):
    """#2459: the contention arm resets the ff-merge, so its page would be false.

    The post says the range was fast-forwarded and sends the operator at `ansible-playbook
    ansible/deploy.yml --tags <svc>`. After a reset the tree is back on `local`, so that
    command redeploys the OLD secret — and it is an operator's own `deploy.sh` holding the
    lock, so they are at a prompt to run it. The page belongs to the tick that leaves the
    range merged, which is the retry.
    """
    config = mixed(settings, tick, APPLYABLE_ROLE, SECRETS)
    tick.playbook_outcomes = [deploy_locks.ServiceLockBusy("service lock busy")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.head == LOCAL, "the ff-merge was undone"
    assert not [post for post in tick.posts if "`secrets.yml` changed" in post]
    assert marker(state_dir, "secrets_alerted_sha") is None
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.head == ORIGIN, "the retry merged and applied"
    secrets_posts = [post for post in tick.posts if "`secrets.yml` changed" in post]
    assert len(secrets_posts) == 1, "the retry owes exactly one page"
    assert marker(state_dir, "secrets_alerted_sha") == ORIGIN


# ── a k8s role the deploy plane applied is not reported as deferred ───────────────────────


def _hand_edited_radarr(settings, tick, *, declared: bool):
    """A range carrying a hand-edited radarr beside a deploy-plane path and an unpromoted bump.

    Args:
        declared: whether radarr has a `platform: k8s` entry on this host. `deploy.yml`
            applies no role that does not, so the answer decides whether the full-run branch
            may treat the role as applied.
    """
    config = mixed(settings, tick, DEPLOY_PLANE, HAND_EDITED_K8S, promote=False)
    if declared:
        tick.declare(DECLARES_SONARR + "  - name: radarr\n    platform: k8s\n")
    return config


def test_a_k8s_role_the_narrowed_plane_applied_is_not_called_unapplied(
    gitops_deploy, tick, settings, state_dir
):
    """#2453: the plane ran `deploy.yml --tags radarr,sonarr`, so "not applied" is false.

    `narrow_broad` maps radarr's own changed path to its tag, so the narrowed list names it
    whenever the range also carries a deploy-plane path. The post used to print the very
    command the tick had just run as the remedy.
    """
    config = _hand_edited_radarr(settings, tick, declared=True)
    tick.narrow = (0, "radarr,sonarr")
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [[*DEPLOY_SONARR[:-1], "radarr,sonarr"]]
    assert marker(state_dir, "k8s_alerted_sha") is None
    assert not [post for post in tick.posts if "radarr" in post]


def test_a_k8s_role_a_refused_narrowing_applied_is_not_called_unapplied(
    gitops_deploy, tick, settings, state_dir
):
    """The other applying branch: a refused narrowing runs the whole `deploy.yml`.

    `_deploy_plane` takes a full run on any doubt, which applies every declared k8s entry —
    radarr among them. The subtraction has to cover this branch too, or the common case of a
    range the derivation cannot narrow keeps posting the false "not applied".
    """
    config = _hand_edited_radarr(settings, tick, declared=True)
    tick.narrow = (3, "")
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [DEPLOY_PLANE_FULL]
    assert marker(state_dir, "k8s_alerted_sha") is None


def test_a_k8s_role_this_host_does_not_declare_is_still_called_unapplied(
    gitops_deploy, tick, settings, state_dir
):
    """The rejecting half for the full run: `deploy.yml` applies no undeclared role.

    `covered_by_plane` returns the WHOLE set when a plan carries no tags, so without the
    intersection against the declared entries a role with no `containers_list` entry here
    would be subtracted from the post and named nowhere at all.
    """
    config = _hand_edited_radarr(settings, tick, declared=False)
    tick.narrow = (3, "")
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [DEPLOY_PLANE_FULL]
    assert marker(state_dir, "k8s_alerted_sha") == ORIGIN
    assert any("radarr" in post for post in tick.posts)


def test_a_k8s_role_the_deploy_plane_missed_is_still_called_unapplied(
    gitops_deploy, tick, settings, state_dir
):
    """The rejecting half for the narrowed run: a plane narrowed elsewhere covers nothing."""
    config = _hand_edited_radarr(settings, tick, declared=True)
    tick.narrow = (0, "jellyfin")
    assert gitops_deploy.main(tick.tools, config) == 0
    assert marker(state_dir, "k8s_alerted_sha") == ORIGIN
    assert any("radarr" in post for post in tick.posts)


def test_a_budget_deferred_bump_is_still_named_by_the_deferral_post(
    gitops_deploy, tick, settings, state_dir
):
    """The subtraction above must not reach the one signal a budget-deferred bump has (#2449).

    A bump the remaining budget cannot fit is folded into `cs.k8s`, and it is there precisely
    BECAUSE no plan applied it — so it can never be plane-covered. Losing it from the post
    would leave the bump merged, unapplied and named nowhere.
    """
    config = dataclasses.replace(
        mixed(settings, tick, DEPLOY_PLANE),
        broad_deploy_timeout_s=60,
        k8s_deploy_timeout_s=900,
    )
    tick.narrow = (0, "jellyfin")
    assert gitops_deploy.main(tick.tools, config) == 0
    assert marker(state_dir, "k8s_alerted_sha") == ORIGIN
    assert any("sonarr" in post for post in tick.posts)
