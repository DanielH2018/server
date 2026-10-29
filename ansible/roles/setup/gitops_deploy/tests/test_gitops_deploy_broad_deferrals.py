#!/usr/bin/env python3
"""What a broad tick SAYS about the half of its range it did not deploy.

The sibling suite (`test_gitops_deploy_broad_k8s.py`) covers what a broad range deploys. This
one covers the pages it sends about everything else: a rotation's page after a failed apply,
a k8s role the tick's own deploy plane applied, and the budget-deferred bump whose only
signal is the post the other two rules must not take away.

Each rule is a pair, for the reason the sibling suite states: a channel that fired on every
range and one that fired on none read the same from the accepting side alone.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_deferrals.py
"""

import dataclasses

import pytest

from deployer_lib import deploy_locks
from gitops_ledger import (
    OWED_K8S_DEFERRED,
    OWED_K8S_UNAPPLIED,
    parse_owed,
    parse_receipts,
)
from _broad_k8s_range import (
    alerted,
    APPLY_GITOPS_DEPLOY,
    APPLYABLE_ROLE,
    DECLARES_SONARR,
    DEPLOY_PLANE,
    DEPLOY_SONARR,
    HAND_EDITED_K8S,
    LOCAL,
    ORIGIN,
    SECRETS,
    marker,
    mixed,
    plane_applies_radarr,
)


def test_a_failed_broad_apply_still_pages_a_secret_riding_the_same_range(
    gitops_deploy, tick, settings, state_dir, state
):
    """Every arm that leaves the range merged sends the page itself.

    `alert_once` advances its marker on DETECTION and the range has already fast-forwarded, so
    `local == origin` and every later tick noops. A page skipped here is never sent, and the
    rotated value sits merged and stale in every consumer with nothing naming it.
    """
    config = mixed(settings, tick, APPLYABLE_ROLE, SECRETS)
    tick.playbook_outcomes = [RuntimeError("the setup plane blew up")]
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert alerted(state_dir, "secrets") == ORIGIN
    assert any("`secrets.yml` changed" in post for post in tick.posts)


def test_a_failed_broad_apply_pages_no_secret_the_range_never_carried(
    gitops_deploy, tick, settings, state_dir, state
):
    """The rejecting half: firing on the failure arm must not mean firing unconditionally."""
    config = mixed(settings, tick, APPLYABLE_ROLE)
    tick.playbook_outcomes = [RuntimeError("the setup plane blew up")]
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert alerted(state_dir, "secrets") is None
    assert not [post for post in tick.posts if "`secrets.yml` changed" in post]


def test_a_contended_tick_pages_no_secret_until_the_retry_merges(
    gitops_deploy, tick, settings, state_dir, state
):
    """The contention arm resets the ff-merge, so its page would be false.

    The post says the range was fast-forwarded and sends the operator at `ansible-playbook
    ansible/deploy.yml --tags <svc>`. After a reset the tree is back on `local`, so that
    command redeploys the OLD secret — and it is an operator's own `deploy.sh` holding the
    lock, so they are at a prompt to run it. The page belongs to the tick that leaves the
    range merged, which is the retry.
    """
    config = mixed(settings, tick, APPLYABLE_ROLE, SECRETS)
    tick.playbook_outcomes = [deploy_locks.ServiceLockBusy("service lock busy")]
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert tick.head == LOCAL, "the ff-merge was undone"
    assert not [post for post in tick.posts if "`secrets.yml` changed" in post]
    assert alerted(state_dir, "secrets") is None
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert tick.head == ORIGIN, "the retry merged and applied"
    secrets_posts = [post for post in tick.posts if "`secrets.yml` changed" in post]
    assert len(secrets_posts) == 1, "the retry owes exactly one page"
    assert alerted(state_dir, "secrets") == ORIGIN


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
    gitops_deploy, tick, settings, state_dir, state
):
    """The plane ran `deploy.yml --tags radarr,sonarr`, so "not applied" is false.

    `narrow_broad` maps radarr's own changed path to its tag, so the narrowed list names it
    whenever the range also carries a deploy-plane path. Otherwise the post would print the
    very command the tick had just run as the remedy.
    """
    config = _hand_edited_radarr(settings, tick, declared=True)
    tick.narrow = (0, "radarr,sonarr")
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert tick.playbooks == [[*DEPLOY_SONARR[:-1], "radarr,sonarr"]]
    assert alerted(state_dir, "k8s") is None
    assert not [post for post in tick.posts if "radarr" in post]


@pytest.mark.parametrize("declared", [True, False])
def test_a_k8s_role_beside_a_refused_narrowing_is_called_unapplied(
    gitops_deploy, tick, settings, state_dir, state, declared
):
    """A refused narrowing runs nothing since #4333, so it covers no role in the range.

    Until then it ran the whole `deploy.yml`, and a declared role was subtracted as applied.
    """
    config = _hand_edited_radarr(settings, tick, declared=declared)
    tick.narrow = (3, "")
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert tick.playbooks == []
    assert alerted(state_dir, "k8s") == ORIGIN
    assert any("radarr" in post for post in tick.posts)
    owed = {e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)}
    assert "radarr" in owed


def test_a_k8s_role_the_deploy_plane_missed_is_still_called_unapplied(
    gitops_deploy, tick, settings, state_dir, state
):
    """The rejecting half for the narrowed run: a plane narrowed elsewhere covers nothing."""
    config = _hand_edited_radarr(settings, tick, declared=True)
    tick.narrow = (0, "jellyfin")
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert alerted(state_dir, "k8s") == ORIGIN
    assert any("radarr" in post for post in tick.posts)


def test_a_budget_deferred_bump_is_still_named_by_the_deferral_post(
    gitops_deploy, tick, settings, state_dir, state
):
    """The subtraction above must not reach the one signal a budget-deferred bump has.

    A bump the remaining budget cannot fit is folded into `cs.k8s`, and it is there precisely
    BECAUSE no plan applied it — so it can never be plane-covered. Losing it from the post
    would leave the bump merged, unapplied and named nowhere.
    """
    assert gitops_deploy.main(tick.tools, _out_of_budget(settings, tick), state) == 0
    assert alerted(state_dir, "k8s") == ORIGIN
    assert any("sonarr" in post for post in tick.posts)


# ── the k8s_deferred class: the durable half of a budget deferral ─────────────────


def deferred(state_dir):
    """The `owed` ledger's `k8s_deferred` entries, which every reader pages on (#3392)."""
    return parse_owed(marker(state_dir, "owed.jsonl"), OWED_K8S_DEFERRED)


def _out_of_budget(settings, tick):
    """A mixed range whose deploy plane leaves too little budget for the sonarr bump.

    The plane is narrowed to a service the range does not carry, so it applies and covers
    nothing — the bump is left to `apply_broad_k8s`, which finds the broad deadline already
    inside `k8s_deploy_timeout_s` and defers.
    """
    config = dataclasses.replace(
        mixed(settings, tick, DEPLOY_PLANE),
        broad_deploy_timeout_s=60,
        k8s_deploy_timeout_s=900,
    )
    tick.narrow = (0, "jellyfin")
    return config


def test_a_budget_deferred_bump_is_recorded_in_the_k8s_deferred_class(
    gitops_deploy, tick, settings, state_dir, state
):
    """The post fires once and the range is merged, so the marker is the durable half.

    `Release Staleness Drift` reads the unapplied pin too, but that monitor goes DOWN for any
    stale record in the fleet — a new deferral adds nothing an operator can see on a tile that
    is already red. The marker pages GitOps Deploy — Status on its own age instead.
    """
    assert gitops_deploy.main(tick.tools, _out_of_budget(settings, tick), state) == 0
    [entry] = deferred(state_dir)
    assert (entry.origin, entry.subject) == (ORIGIN, "sonarr")
    assert entry.at > 0, "the first-seen stamp is what monitor-bridge pages on"


def test_a_bump_the_tick_deployed_is_not_recorded(
    gitops_deploy, tick, settings, state_dir, state
):
    """The rejecting half: recording every promoted bump would page on the happy path."""
    config = mixed(settings, tick, DEPLOY_PLANE)
    tick.narrow = (0, "jellyfin")
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert tick.playbooks[-1] == DEPLOY_SONARR, "the bump was deployed, not deferred"
    assert deferred(state_dir) == []


def test_the_service_deploy_a_later_tick_runs_clears_the_marker(
    gitops_deploy, tick, settings, state_dir, state
):
    """The way out the deployer owns. Without it the page never stops.

    An operator's own `./scripts/deploy.sh` is invisible here, which is what
    `gitops_state.py clear-owed k8s_deferred` exists for; a deploy the TICK runs is not.
    """
    assert gitops_deploy.main(tick.tools, _out_of_budget(settings, tick), state) == 0
    assert deferred(state_dir) != []
    tick.head = LOCAL
    assert gitops_deploy.main(tick.tools, mixed(settings, tick), state) == 0
    assert tick.playbooks[-1] == DEPLOY_SONARR, "the retry deployed the bump"
    assert deferred(state_dir) == []


def test_a_deploy_plane_that_applies_the_service_clears_the_marker(
    gitops_deploy, tick, settings, state_dir, state
):
    """The second way out: a later broad range's plane applies the service on its own.

    The pending set is asked rather than the range, because the tick that deferred the bump
    merged it — no later `local..origin` carries that commit.
    """
    (state_dir / "owed.jsonl").write_text(
        f'{{"at": 1000, "class": "k8s_deferred", "origin": "{"9" * 40}", "subject": "radarr"}}\n'
    )
    assert (
        gitops_deploy.main(tick.tools, plane_applies_radarr(settings, tick), state) == 0
    )
    assert tick.playbooks[0] == [*DEPLOY_SONARR[:-1], "radarr"], "the plane ran"
    assert deferred(state_dir) == []


def test_a_hand_edited_k8s_role_is_not_recorded(
    gitops_deploy, tick, settings, state_dir, state
):
    """The class the marker does NOT cover.

    A hand-edited role is merged by the person landing it, whose `land.sh` is watching — and
    recording every such change would hold GitOps Deploy — Status red as normal operation.
    """
    config = mixed(settings, tick, APPLYABLE_ROLE, HAND_EDITED_K8S)
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert alerted(state_dir, "k8s") == ORIGIN, "it took the defer-and-alert path"
    assert deferred(state_dir) == []


# ── the deferred receipt is written only after the ledger lines it counts ────────────────


def _read_during_the_bump_deploy(settings, tick, state, outcome):
    """A refused deploy plane beside a sonarr bump the tick deploys, with radarr declared.

    Returns the `Config` and a list that receives, as the bump's playbook starts, each service
    the receipt names under `deferred` with no ledger line yet. That deploy is the
    minutes-long window a landing whose tick watch was abandoned reads the markers in.

    Args:
        outcome: an exception the bump deploy raises, or None for a clean one.
    """
    config = mixed(settings, tick, DEPLOY_PLANE)
    tick.declare(DECLARES_SONARR + "  - name: radarr\n    platform: k8s\n")
    tick.narrow = (3, "")
    tick.playbook_outcomes = [outcome]
    uncounted: list[str] = []
    run = tick.tools.run

    def read_then_run(argv, **kwargs):
        if "ansible-playbook" in argv:
            lines = parse_owed(state.read("owed"), OWED_K8S_UNAPPLIED)
            named = {
                s
                for r in parse_receipts(state.read("receipts"))
                for s in r.deferred.get("ansible/deploy.yml", ())
            }
            uncounted.extend(sorted(named - {e.subject for e in lines}))
        return run(argv, **kwargs)

    tick.tools = dataclasses.replace(tick.tools, run=read_then_run)
    return config, uncounted


@pytest.mark.parametrize("outcome", [None, RuntimeError("boom")])
def test_a_deferred_receipt_never_names_a_service_the_ledger_does_not_yet_hold(
    gitops_deploy, tick, settings, state, outcome
):
    """#4866: a read during the bump deploy counted 0 owed and settled the landing.

    The receipt's `deferred` entry was written in the plans loop, and the lines it counts only
    after the bump deploy. A reader between the two found every owed service discharged. The
    entry is still written on both merged exits, so a fix-forward's land.sh finds the deferral.
    """
    config, uncounted = _read_during_the_bump_deploy(settings, tick, state, outcome)
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert tick.playbooks == [DEPLOY_SONARR]
    assert uncounted == []
    (receipt,) = parse_receipts(state.read("receipts"))
    assert receipt.deferred == {"ansible/deploy.yml": frozenset({"radarr", "sonarr"})}


def test_a_role_the_tick_applies_still_owes_the_hosts_its_run_did_not_reach(
    gitops_deploy, tick, settings, state
):
    """A change reaching a host beyond the tick's own is owed there, past the tick's apply.

    `hypervisor` routes onto the tick's host, which runs only its teardown half, so the
    daniel-server install was owed with nothing durable naming it (#6510, #6411). The line is
    written at the ff-merge, before the apply, so it must survive the clear that follows.
    """
    config = mixed(settings, tick, APPLYABLE_ROLE)
    tick.reach = {"gitops_deploy": ("daniel-pi", "daniel-server")}
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert APPLY_GITOPS_DEPLOY in tick.playbooks
    (entry,) = state.manual_plane_pending()
    assert (entry.role, entry.playbook) == (
        "gitops_deploy",
        "ansible/initial_setup.yml",
    )
    assert entry.hosts == ("daniel-pi", "daniel-server")


def test_a_role_whose_change_reaches_only_the_tick_host_owes_nothing(
    gitops_deploy, tick, settings, state
):
    """The rejecting half: the same apply with no other host reached writes no line."""
    config = mixed(settings, tick, APPLYABLE_ROLE)
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert APPLY_GITOPS_DEPLOY in tick.playbooks
    assert state.manual_plane_pending() == []


def test_a_failed_reach_derivation_records_nothing_and_still_applies(
    gitops_deploy, tick, settings, state
):
    """The tick has already fast-forwarded, so a failed derivation must not stop the apply."""
    config = mixed(settings, tick, APPLYABLE_ROLE)
    tick.reach_error = RuntimeError("setup_reach_hosts.py exited 1")
    assert gitops_deploy.main(tick.tools, config, state) == 0
    assert APPLY_GITOPS_DEPLOY in tick.playbooks
    assert state.manual_plane_pending() == []


def test_a_repo_file_hypervisor_copies_is_owed_on_the_host_it_copies_it_to(
    gitops_deploy, tick, settings, state
):
    """The drill script sits under no role, and daniel-server kept the old copy (#6411, #6527).

    `deploy_cross_role` routes it to hypervisor, so the tick applies hypervisor on its own
    host, and the reach names the daniel-server install the tick's run does not reach.
    """
    tick.paths = ["scripts/backup/etcd_restore_drill.sh"]
    tick.reach = {"hypervisor": ("daniel-server",)}
    assert gitops_deploy.main(tick.tools, settings, state) == 0
    assert [*APPLY_GITOPS_DEPLOY[:-1], "hypervisor"] in tick.playbooks
    (entry,) = state.manual_plane_pending()
    assert (entry.role, entry.hosts) == ("hypervisor", ("daniel-server",))
