"""What a broad tick parks on, what it fast-forwards past instead, and what it says either way.

Split out of test_gitops_deploy_main_branches.py, which is at its module-length cap. Every
test here is one half of a pair: one input the branch must fire on and one it must stay silent
on — a guard that fired on every branch and one that fired on none read the same from the
passing side alone.

Two planes used to park identically: a bring-up playbook (`_BROAD_MANUAL_PREFIXES`) and a
setup role `initial_setup.yml` does not include. Only the first still does. The second
fast-forwards and records the role in the `manual_plane` marker, because parking held every
other session's landing behind a role a hand was always going to apply — ten episodes over
the seven days to 2026-09-11, the longest about forty minutes.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_park.py
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_park.py

import deploy_defer

# The SHA the `tick` fixture fast-forwards to; see test_gitops_deploy_main_branches.py for why
# `from conftest import` is avoided.
ORIGIN = "2" * 40

# A setup role applied by `k3s-bringup.yml`, so `setup_tags_for` derives nothing for it.
K3S_SETUP = "ansible/roles/setup/k3s/tasks/longhorn-backup.yml"
# A bring-up playbook: the plane the deployer must never apply itself.
BRINGUP = "ansible/k3s-bringup.yml"
# A deploy-plane path, for the range that carries both halves.
GROUP_VARS = "ansible/inventory/group_vars/all.yml"


# ── the plane that still parks ─────────────────────────────────────────────────────────
def test_a_bring_up_playbook_parks_and_names_its_reason_on_every_tick(
    gitops_deploy, tick, capsys
):
    """The bring-up playbooks run by hand by construction, so the tick must not ff-merge.

    The Discord page is throttled once per SHA. Before #1467 the journal was throttled with
    it, so nine commits sat unmerged for twenty minutes with no per-tick line naming a reason.
    """
    tick.paths = [BRINGUP]
    assert gitops_deploy.main(tick.tools) == 0
    assert gitops_deploy.main(tick.tools) == 0, "the range is unchanged; it re-evals"
    out = capsys.readouterr().out
    assert out.count("parked, nothing merged") == 2, "the journal is not throttled"
    assert "runs by hand by construction" in out
    assert tick.merges == [] and tick.playbooks == []
    assert len(tick.posts) == 1, "the PAGE is still once per SHA"
    assert gitops_deploy.STATE.manual_plane is None, "a parked plane needs no marker"


def test_a_setup_path_belonging_to_no_role_still_parks(gitops_deploy, tick, capsys):
    """A setup-plane path with no role to name has no hand command to record, so it parks.

    `_note_setup_role` matches `roles/setup/<name>/`, so a file sitting directly under
    `roles/setup/` is broad, unroutable and nameless at once. Fast-forwarding it would apply
    nothing, record nothing and say nothing — the silent-swallow shape this arm exists to
    prevent.
    """
    tick.paths = ["ansible/roles/setup/README.yml"]
    assert gitops_deploy.main(tick.tools) == 0
    out = capsys.readouterr().out
    assert "parked, nothing merged" in out
    assert "names no role" in out, "and the journal says which of the two parks this is"
    assert tick.merges == [] and tick.playbooks == []
    assert gitops_deploy.STATE.manual_plane is None


# ── the plane that now fast-forwards ───────────────────────────────────────────────────
def test_an_unapplyable_setup_role_fast_forwards_and_records_the_marker(
    gitops_deploy, tick, capsys
):
    """The 2026-09-09 park, unparked: the range merges and the role is recorded instead."""
    tick.paths = [K3S_SETUP]
    assert gitops_deploy.main(tick.tools) == 0
    out = capsys.readouterr().out
    assert "parked, nothing merged" not in out
    assert tick.merges == [ORIGIN], "the range is no longer held back"
    assert tick.playbooks == [], "and nothing was applied on its behalf"
    (entry,) = gitops_deploy.STATE.manual_plane_pending()
    assert (entry.origin, entry.playbook, entry.role) == (
        ORIGIN,
        "ansible/k3s-bringup.yml",
        "k3s",
    )
    assert "manual_plane recorded: k3s" in out, "the tick that adds a role says so"
    assert "ansible/k3s-bringup.yml --tags k3s" in out
    assert len(tick.posts) == 1, "one page per SHA, naming the hand command"
    assert "k3s-bringup.yml" in tick.posts[0]
    assert gitops_deploy.STATE.broad_applied is None, (
        "nothing was applied, so the marker that says one was must stay empty"
    )


def test_a_pending_role_is_named_on_every_later_tick(gitops_deploy, tick, capsys):
    """The marker is durable, so the journal has to keep saying it is there.

    After the ff-merge the tick converges and never re-enters the broad arm, so a line
    written only where the marker is written would appear once and never again — and an
    operator reading `journalctl -t gitops-deploy` an hour later would see an idle deployer.
    """
    tick.paths = [K3S_SETUP]
    assert gitops_deploy.main(tick.tools) == 0
    capsys.readouterr()
    assert gitops_deploy.main(tick.tools) == 0, "converged: an idle tick"
    out = capsys.readouterr().out
    assert "manual_plane pending: k3s" in out
    assert "ansible/k3s-bringup.yml --tags k3s" in out


def test_a_role_already_recorded_is_not_announced_again(gitops_deploy, tick, capsys):
    """A second range naming the same role adds no line: `main()` already named the set.

    Both call sites logged the whole pending set, so a role already listed was printed twice
    on the tick that re-recorded it — once as pending, once again right after.
    """
    config = gitops_deploy.tick_config()
    deploy_defer.record(tick.tools, gitops_deploy.STATE, config, ORIGIN, ["k3s"])
    capsys.readouterr()
    deploy_defer.record(tick.tools, gitops_deploy.STATE, config, ORIGIN, ["k3s"])
    assert "manual_plane recorded" not in capsys.readouterr().out
    assert len(gitops_deploy.STATE.manual_plane_pending()) == 1, "and no second line"


def test_an_idle_tick_with_no_pending_role_says_nothing(gitops_deploy, tick, capsys):
    """The rejecting half: the per-tick line must not fire on an empty marker."""
    assert gitops_deploy.main(tick.tools) == 0
    assert "manual_plane pending" not in capsys.readouterr().out


def test_a_mixed_range_applies_the_deploy_plane_and_still_records_the_role(
    gitops_deploy, tick
):
    """A range carrying both halves: the applyable one is applied, the other is recorded.

    Parking used to hold BOTH, so an inventory change sharing a push with a `roles/setup/k3s/`
    change waited on the hand that would never come for it.
    """
    tick.paths = [GROUP_VARS, K3S_SETUP]
    tick.narrow = (0, "sonarr")
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks[0][-3:] == ["ansible/deploy.yml", "--tags", "sonarr"]
    assert [e.role for e in gitops_deploy.STATE.manual_plane_pending()] == ["k3s"]
    assert gitops_deploy.STATE.broad_applied == f"{ORIGIN} ansible/deploy.yml sonarr", (
        "the half that WAS applied still records it"
    )


def test_a_setup_role_the_deployer_can_apply_logs_no_park(gitops_deploy, tick, capsys):
    """The rejecting half for the marker too: a resolvable role applies and records nothing."""
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    assert gitops_deploy.main(tick.tools) == 0
    out = capsys.readouterr().out
    assert "parked, nothing merged" not in out
    assert "manual_plane pending" not in out
    assert tick.merges == [ORIGIN] and tick.playbooks
    assert gitops_deploy.STATE.manual_plane is None


def test_a_failed_apply_still_records_the_role(gitops_deploy, tick):
    """The range is MERGED before the apply, so the role is owed a hand either way.

    Recorded after the apply, the failure path's early return skipped it: the tick held the
    SHA, the k3s change sat fast-forwarded on disk with no marker, and once the operator
    fixed forward and origin advanced past the hold, `local..origin` no longer carried that
    commit — the broad arm would never see the role again.
    """
    tick.paths = [GROUP_VARS, K3S_SETUP]
    tick.narrow = (0, "sonarr")
    tick.playbook_outcomes = [RuntimeError("boom")]
    gitops_deploy.main(tick.tools)
    assert [e.role for e in gitops_deploy.STATE.manual_plane_pending()] == ["k3s"]
    assert gitops_deploy.STATE.broad_applied is None
    assert gitops_deploy.STATE.hold_sha == ORIGIN, "the failed apply is still held"
