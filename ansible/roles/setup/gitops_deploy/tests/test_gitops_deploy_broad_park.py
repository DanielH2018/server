"""What a broad tick parks on, what it fast-forwards past instead, and what it says either way.

Every test here is one half of a pair: one input the branch must fire on and one it must stay silent
on — a guard that fired on every branch and one that fired on none read the same from the
passing side alone.

Two planes differ in how the tick treats them. A bring-up playbook (`_BROAD_MANUAL_PREFIXES`)
parks. A setup role `initial_setup.yml` does not include fast-forwards and is recorded in the
`owed` ledger's `manual_plane` class, because parking would hold every other session's landing behind a role
a hand is always going to apply.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_park.py
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_park.py

import deploy_defer
import deploy_locks
from deploy_tick_types import TickTarget
from _deploy_fakes import receipt_applied
from gitops_ledger import parse_receipts

# The two SHAs the `tick` fixture bounds a range with; see test_gitops_deploy_main_branches.py
# for why `from conftest import` is avoided.
LOCAL = "1" * 40
ORIGIN = "2" * 40
# The range `deploy_defer.record` hands the narrowing, which the scripted fake asserts against.
TARGET = TickTarget(
    local=LOCAL, origin=ORIGIN, hold=None, dirty=False, status="", action="deploy"
)

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

    The Discord page is throttled once per SHA. The journal is not throttled with it, so every
    tick prints a line naming the reason.
    """
    tick.paths = [BRINGUP]
    assert gitops_deploy.main(tick.tools) == 0
    assert gitops_deploy.main(tick.tools) == 0, "the range is unchanged; it re-evals"
    out = capsys.readouterr().out
    assert out.count("parked, nothing merged") == 2, "the journal is not throttled"
    assert "runs by hand by construction" in out
    assert tick.merges == [] and tick.playbooks == []
    assert len(tick.posts) == 1, "the PAGE is still once per SHA"
    assert gitops_deploy.STATE.manual_plane_pending() == [], (
        "a parked plane needs no marker"
    )


def test_a_setup_path_belonging_to_no_role_still_parks(gitops_deploy, tick, capsys):
    """A setup-plane path with no role to name has no hand command to record, so it parks.

    `setup_roles_for` matches `roles/setup/<name>/`, so a file sitting directly under
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
    assert gitops_deploy.STATE.manual_plane_pending() == []


# ── the plane that fast-forwards ───────────────────────────────────────────────────
def test_an_unapplyable_setup_role_fast_forwards_and_records_the_marker(
    gitops_deploy, tick, capsys
):
    """The range merges and the role is recorded instead of parking."""
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
    assert not receipt_applied(gitops_deploy.STATE), (
        "nothing was applied, so the receipt must not say one was"
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


# ── the recorded role's own narrow tag, on every surface that quotes the deferral ───
# A range that changes `templates/readonly-rbac.yaml.j2` needs `--tags kubeconfig`, not
# `--tags k3s`, which restarts the control plane.
RBAC = "ansible/roles/setup/k3s/templates/readonly-rbac.yaml.j2"


def test_a_narrowed_role_is_recorded_with_its_tag_and_quoted_everywhere(
    gitops_deploy, tick, capsys
):
    """The journal line, the Discord page and the next tick's pending line all narrow."""
    tick.paths = [RBAC]
    tick.narrow_setup["k3s"] = (0, "kubeconfig")
    assert gitops_deploy.main(tick.tools) == 0
    out = capsys.readouterr().out
    assert gitops_deploy.STATE.manual_plane_tags_pending() == {
        "k3s": frozenset({"kubeconfig"})
    }
    assert "ansible/k3s-bringup.yml --tags kubeconfig" in out
    assert "--tags k3s`" not in out
    assert "--tags kubeconfig" in tick.posts[0]
    capsys.readouterr()
    assert gitops_deploy.main(tick.tools) == 0, "converged: an idle tick"
    assert "--tags kubeconfig" in capsys.readouterr().out, (
        "the per-tick pending line reads the same marker"
    )


def test_a_refused_narrowing_records_nothing_and_keeps_the_role_tag(
    gitops_deploy, tick, capsys
):
    """The rejecting half: a derivation that cannot answer must widen, not narrow.

    `narrow_setup` unscripted is a non-zero exit, which is what the real script returns for
    every shape it refuses — an untagged task file, a variable nothing in the role reads.
    """
    tick.paths = [K3S_SETUP]
    assert gitops_deploy.main(tick.tools) == 0
    out = capsys.readouterr().out
    assert gitops_deploy.STATE.manual_plane_tags_pending() == {"k3s": frozenset()}
    assert "ansible/k3s-bringup.yml --tags k3s" in out
    assert "WARNING" in out, (
        "and the warning about what that tag does comes back with it"
    )


def test_a_second_narrowable_range_on_a_role_with_a_row_unions_the_tags(
    gitops_deploy, tick, capsys
):
    """The accepting half of the upgrade case: a row the deployer wrote is extended, not reset.

    A line recorded WITH a row tells the second range what the first one needed, so the union
    of the two is the complete answer and stays narrow.
    """
    config = gitops_deploy.tick_config()
    tick.narrow_setup["k3s"] = (0, "coredns")
    deploy_defer.record(tick.tools, gitops_deploy.STATE, config, TARGET, ["k3s"])
    tick.narrow_setup["k3s"] = (0, "kubeconfig")
    deploy_defer.record(tick.tools, gitops_deploy.STATE, config, TARGET, ["k3s"])
    assert gitops_deploy.STATE.manual_plane_tags_pending() == {
        "k3s": frozenset({"coredns", "kubeconfig"})
    }


def test_a_role_pending_with_no_row_stays_at_the_role_tag(gitops_deploy, tick, capsys):
    """A pending line nobody narrowed must not be narrowed by the NEXT range's answer.

    A `manual_plane` line can carry no tags, as `record_manual_plane` writes it before any
    derivation answers. Whatever made that line pending is unknown here, so a second
    range answering `kubeconfig` would print `--tags kubeconfig` and leave the first range's
    change unapplied behind a clear command. Unknown absorbs anything: the role tag.

    The precondition is what separates this from the refusal path. The role tag
    prints whether the derivation ANSWERED `kubeconfig` and was absorbed, or never answered at
    all: a raise, a non-zero exit and an empty answer all yield the same tag. So the test first
    asks `narrow_tags_for` itself, on this tick's range, and needs `kubeconfig` back.
    Without it, a derivation that refused every answer would leave this test green.
    """
    gitops_deploy.STATE.record_manual_plane(
        LOCAL, "ansible/k3s-bringup.yml", "k3s", 1000.0
    )
    tick.paths = [RBAC]
    tick.narrow_setup["k3s"] = (0, "kubeconfig")
    answered = deploy_defer.narrow_tags_for(
        tick.tools, gitops_deploy.tick_config(), TARGET, "k3s"
    )
    assert answered == frozenset({"kubeconfig"}), (
        "the derivation refused, so the role tag below would be the refusal's answer rather "
        "than an absorbed narrowing, and this test would pass for either"
    )
    assert gitops_deploy.main(tick.tools) == 0
    out = capsys.readouterr().out
    assert gitops_deploy.STATE.manual_plane_tags_pending() == {"k3s": frozenset()}
    assert "ansible/k3s-bringup.yml --tags k3s" in out
    assert "--tags kubeconfig" not in out


def test_a_narrowing_that_raises_keeps_the_role_tag(gitops_deploy, tick, capsys):
    """`narrow_tags_for`'s `except Exception` arm: a crash is a refusal, never an escape.

    The call decodes a subprocess's output, so it can raise a `UnicodeDecodeError` that is no
    `SubprocessError`. An escape here would fail a tick that has already fast-forwarded.
    """
    tick.paths = [RBAC]
    tick.narrow_setup_error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")
    assert gitops_deploy.main(tick.tools) == 0
    out = capsys.readouterr().out
    assert "narrow-setup: k3s not narrowed (UnicodeDecodeError" in out
    assert gitops_deploy.STATE.manual_plane_tags_pending() == {"k3s": frozenset()}
    assert "ansible/k3s-bringup.yml --tags k3s" in out


def test_a_role_already_recorded_is_not_announced_again(gitops_deploy, tick, capsys):
    """A second range naming the same role adds no line: `main()` already named the set.

    Both call sites logged the whole pending set, so a role already listed was printed twice
    on the tick that re-recorded it — once as pending, once again right after.
    """
    config = gitops_deploy.tick_config()
    deploy_defer.record(tick.tools, gitops_deploy.STATE, config, TARGET, ["k3s"])
    capsys.readouterr()
    deploy_defer.record(tick.tools, gitops_deploy.STATE, config, TARGET, ["k3s"])
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

    Parking the range would hold BOTH, so an inventory change sharing a push with a
    `roles/setup/k3s/` change would wait on a hand that never comes for it.
    """
    tick.paths = [GROUP_VARS, K3S_SETUP]
    tick.narrow = (0, "sonarr")
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks[0][-3:] == ["ansible/deploy.yml", "--tags", "sonarr"]
    assert [e.role for e in gitops_deploy.STATE.manual_plane_pending()] == ["k3s"]
    assert receipt_applied(gitops_deploy.STATE) == {
        "ansible/deploy.yml": ("sonarr",)
    }, "the half that WAS applied still records it"


def test_a_mixed_range_writes_one_receipt_naming_both_halves(gitops_deploy, tick):
    """The receipt `land.sh` reads instead of re-deriving (#3391): the plane the tick applied,
    with its tags, and the role it left to a hand, under the one origin SHA it crossed to."""
    tick.paths = [GROUP_VARS, K3S_SETUP]
    tick.narrow = (0, "sonarr")
    assert gitops_deploy.main(tick.tools) == 0
    (receipt,) = parse_receipts(gitops_deploy.STATE.read("receipts"))
    assert receipt.origin == ORIGIN
    assert receipt.applied == {"ansible/deploy.yml": ("sonarr",)}
    assert list(receipt.manual) == ["k3s"]


def test_a_setup_role_the_deployer_can_apply_logs_no_park(gitops_deploy, tick, capsys):
    """The rejecting half for the marker too: a resolvable role applies and records nothing."""
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    assert gitops_deploy.main(tick.tools) == 0
    out = capsys.readouterr().out
    assert "parked, nothing merged" not in out
    assert "manual_plane pending" not in out
    assert tick.merges == [ORIGIN] and tick.playbooks
    assert gitops_deploy.STATE.manual_plane_pending() == []


def test_a_failed_apply_still_records_the_role(gitops_deploy, tick):
    """The range is MERGED before the apply, so the role is owed a hand either way.

    Recording after the apply would let the failure path's early return skip it: the tick
    holds the SHA, the k3s change sits fast-forwarded on disk with no marker, and once the
    operator fixes forward and origin advances past the hold, `local..origin` no longer
    carries that commit — the broad arm never sees the role again.
    """
    tick.paths = [GROUP_VARS, K3S_SETUP]
    tick.narrow = (0, "sonarr")
    tick.playbook_outcomes = [RuntimeError("boom")]
    gitops_deploy.main(tick.tools)
    assert [e.role for e in gitops_deploy.STATE.manual_plane_pending()] == ["k3s"]
    assert not receipt_applied(gitops_deploy.STATE)
    assert gitops_deploy.STATE.hold_sha == ORIGIN, "the failed apply is still held"


# ── a rolled-back tick takes back exactly what it wrote, and no more ─────────────


def test_a_rolled_back_tick_leaves_an_earlier_ranges_tag_standing(gitops_deploy, tick):
    """The row this tick WIDENED goes back to what it was; the earlier range's tag survives.

    `record` writes the tags for every role it is handed, including one an earlier
    range already made pending, while `unrecord` takes back the roles whose LINE it
    appended. `unrecord` therefore has to restore the row as it was: leaving `coredns` would
    keep a tag no merged tree carries, and a whole-line reverse would drop the first range's
    line while its change is still merged.
    """
    config = gitops_deploy.tick_config()
    state = gitops_deploy.STATE
    tick.narrow_setup["k3s"] = (0, "kubeconfig")
    deploy_defer.record(tick.tools, state, config, TARGET, ["k3s"])
    tick.narrow_setup["k3s"] = (0, "coredns")
    recorded = deploy_defer.record(tick.tools, state, config, TARGET, ["k3s"])
    assert state.manual_plane_tags_pending() == {
        "k3s": frozenset({"coredns", "kubeconfig"})
    }
    deploy_defer.unrecord(state, ORIGIN, recorded)
    assert state.manual_plane_tags_pending() == {"k3s": frozenset({"kubeconfig"})}
    assert [e.role for e in state.manual_plane_pending()] == ["k3s"], (
        "the first range is still merged, so its line stays"
    )


def test_a_rolled_back_tick_takes_its_own_line_and_row_with_it(gitops_deploy, tick):
    """The other half: a role THIS tick made pending leaves nothing behind.

    Without it a fix that only ever restored rows would read identically from the passing
    side, and the marker would page for six hours over a range no tree carries.
    """
    config = gitops_deploy.tick_config()
    state = gitops_deploy.STATE
    tick.narrow_setup["k3s"] = (0, "kubeconfig")
    recorded = deploy_defer.record(tick.tools, state, config, TARGET, ["k3s"])
    deploy_defer.unrecord(state, ORIGIN, recorded)
    assert state.manual_plane_pending() == []
    assert state.manual_plane_tags_pending() == {}
    assert state.alerted_sha("broad") is None, "the page for this SHA goes too"


def test_a_contended_tick_on_an_already_pending_role_keeps_the_earlier_row(
    gitops_deploy, tick, state_dir
):
    """The same sequence through a real tick, which is where the marker is actually written.

    A busy service lock resets the tree to `local`, so the second range stops being merged.
    The tags must read what the FIRST range needed, and nothing else.
    """
    state = gitops_deploy.STATE
    state.record_manual_plane(LOCAL, "ansible/k3s-bringup.yml", "k3s", 1000.0)
    state.record_manual_plane_tags(
        "k3s", frozenset({"kubeconfig"}), line_predates=False
    )
    tick.paths = [GROUP_VARS, K3S_SETUP]
    tick.narrow = (0, "sonarr")
    tick.narrow_setup["k3s"] = (0, "coredns")
    tick.playbook_outcomes = [deploy_locks.ServiceLockBusy("busy")]
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.head == LOCAL, "the ff-merge was undone, so the range is not merged"
    assert state.manual_plane_tags_pending() == {"k3s": frozenset({"kubeconfig"})}
    assert [e.role for e in state.manual_plane_pending()] == ["k3s"]


def test_a_rolled_back_tick_restores_a_row_its_own_refusal_collapsed(
    gitops_deploy, tick
):
    """The case that makes restore, not subtract, the reverse.

    The second range's derivation refuses, so the union collapses the row to the empty set
    — "the whole role". Nothing subtracted from an empty set recovers `kubeconfig`.
    """
    config = gitops_deploy.tick_config()
    state = gitops_deploy.STATE
    tick.narrow_setup["k3s"] = (0, "kubeconfig")
    deploy_defer.record(tick.tools, state, config, TARGET, ["k3s"])
    del tick.narrow_setup["k3s"]
    recorded = deploy_defer.record(tick.tools, state, config, TARGET, ["k3s"])
    assert state.manual_plane_tags_pending() == {"k3s": frozenset()}
    deploy_defer.unrecord(state, ORIGIN, recorded)
    assert state.manual_plane_tags_pending() == {"k3s": frozenset({"kubeconfig"})}


def test_a_rolled_back_tick_on_an_already_pending_role_pages_once(gitops_deploy, tick):
    """No line appended means nothing to take back from the dedupe page either.

    The role stays pending through the rollback, so clearing the `broad` alert slot there would
    re-page the same SHA on every contended tick. The rejecting half is
    `test_a_rolled_back_tick_takes_its_own_line_and_row_with_it`, where the page does go.
    """
    config = gitops_deploy.tick_config()
    state = gitops_deploy.STATE
    state.record_manual_plane(LOCAL, "ansible/k3s-bringup.yml", "k3s", 1000.0)
    tick.narrow_setup["k3s"] = (0, "kubeconfig")
    for _ in range(2):
        recorded = deploy_defer.record(tick.tools, state, config, TARGET, ["k3s"])
        assert recorded.roles == [], "the role was already pending"
        deploy_defer.unrecord(state, ORIGIN, recorded)
    assert len(tick.posts) == 1
    assert state.alerted_sha("broad") == ORIGIN
