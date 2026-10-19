"""Step 6: the health gate, the half `ansible-playbook` exiting 0 cannot speak to.

Run: uv run pytest scripts/deploy_tools/tests/test_land_health_verdict.py
"""

from dataclasses import replace
from pathlib import Path

import pytest

from _land_fakes import MERGE_SHA, Fakes, receipt
from gitops_ledger import OWED_K8S_UNAPPLIED, owed_line
from gitops_markers import MARKERS
from deploy_tools.deploy_lib.detach_notify import GateResult
from deploy_tools.land_lib import health_verdict, outcome
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.landing import Classification


# The deployer's record of a broad apply that CONTAINS this PR. A self-applied landing needs
# it before it may settle: `behind_since` empty proves only that local == origin.
APPLIED = receipt({"ansible/initial_setup.yml": ["renovate_agent"]})


def _deployed(landing, fakes=None):
    ln, calls = landing(fakes)
    ln.merge_sha, ln.resolved_tags = MERGE_SHA, ["sonarr"]
    ln.classification = Classification(
        plane=(fakes or Fakes()).plane,
        self_applied=(fakes or Fakes()).self_applied,
        self_applied_command=(fakes or Fakes()).self_applied_command,
        remaining_setup=(fakes or Fakes()).remaining_setup,
    )
    return ln, calls


def test_settled_after_a_healthy_deploy(landing, capsys):
    ln, calls = _deployed(landing)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "settled")
    assert exc.value.detail == f"PR #999, {MERGE_SHA}, tags: sonarr"
    assert next(c for c in calls if c[0] == "gate")[1] == (["sonarr"],)
    assert "sonarr: healthy" in capsys.readouterr().out
    # A passing gate never asks: the question can wait out another deploy.
    assert not any(c[0] == "later_deploys" for c in calls)


def test_a_workload_a_later_deploy_re_rolled_is_gated_again_not_failed(landing):
    """#3812: a second deploy of the same service rolled the pods under the first sample."""
    fakes = Fakes(
        gate=(False, ["sonarr: 0/1 ready, 0 updated — rollout incomplete"]),
        regate=(True, ["sonarr: healthy"]),
        later_deploys={"sonarr": "c0ffee"},
    )
    ln, calls = _deployed(landing, fakes)
    ln.deploy_ended_at = 1234.0
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "settled")
    assert "re-gated after a later deploy of sonarr" in exc.value.detail
    assert [c[0] for c in calls].count("gate") == 2
    assert ("later_deploys", (["sonarr"], 1234.0), {}) in calls


def test_a_healthy_re_gate_of_a_commit_without_this_pr_stays_unhealthy(landing, capsys):
    """The tick's rollback redeploys an older pin: its pods are healthy, this change is gone."""
    fakes = Fakes(
        gate=(False, ["sonarr: unhealthy"]),
        regate=(True, ["sonarr: healthy"]),
        later_deploys={"sonarr": "0ldc0mm1t"},
        is_ancestor_of={"0ldc0mm1t": 1},
    )
    ln, _ = _deployed(landing, fakes)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "unhealthy")
    assert "0ldc0mm1t, which is not proved to contain" in capsys.readouterr().out


def test_a_failed_gate_no_later_deploy_explains_stays_unhealthy(landing):
    fakes = Fakes(gate=(False, ["sonarr: unhealthy"]), regate=(True, ["sonarr: ok"]))
    ln, calls = _deployed(landing, fakes)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "unhealthy")
    assert [c[0] for c in calls].count("gate") == 1


def test_a_re_rolled_workload_still_broken_on_the_re_gate_stays_unhealthy(landing):
    fakes = Fakes(
        gate=(False, ["sonarr: unhealthy"]),
        regate=(False, ["sonarr: RECENT RESTART"]),
        later_deploys={"sonarr": "c0ffee"},
    )
    ln, _ = _deployed(landing, fakes)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "unhealthy")
    assert "re-gated after a later deploy of sonarr" in exc.value.detail


def test_a_joined_kick_is_asked_again_after_the_gate_before_any_verdict(landing):
    """The gate is the wait that lets the joined run end, so the second request
    goes out after it and before the verdict -- on the unhealthy exit as well."""
    ln, calls = _deployed(landing, Fakes(gate=(False, ["sonarr: unhealthy"]), tick=[0]))
    ln.ledger.kick = "joined"
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    names = [c[0] for c in calls]
    assert names.index("gate") < names.index("tick")
    assert ln.ledger.kick == "rearmed"


def test_unhealthy_when_the_gate_fails(landing):
    ln, _ = _deployed(landing, Fakes(gate=(False, ["sonarr: unhealthy"])))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "unhealthy")


def _gates(ln, *results):
    """Answer the landing's successive gates with `results`, which may be unreadable."""
    answers = iter(results)
    ln.tools = replace(ln.tools, gate=lambda tags, cwd=None: next(answers))


NOT_GRADED = ["sonarr: not graded -- probe.py health could not read its store"]


def test_an_unreadable_gate_defers_without_asking_about_later_deploys(landing, capsys):
    """#5543: no reading is no failure, and nothing rolled under a gate that saw nothing."""
    ln, calls = _deployed(landing, Fakes(later_deploys={"sonarr": "c0ffee"}))
    _gates(ln, GateResult(False, NOT_GRADED, unreadable=True))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "deferred")
    assert "could not read its store" in exc.value.detail
    assert outcome.UNREADABLE_GATE_NOTE in capsys.readouterr().out
    assert not any(c[0] == "later_deploys" for c in calls)


def test_a_re_gate_that_cannot_read_defers_rather_than_failing(landing):
    ln, _ = _deployed(landing, Fakes(later_deploys={"sonarr": "c0ffee"}))
    _gates(
        ln,
        GateResult(False, ["sonarr: 0/1 ready, 0 updated — rollout incomplete"]),
        GateResult(False, NOT_GRADED, unreadable=True),
    )
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "deferred")
    assert "re-gated after a later deploy of sonarr" in exc.value.detail


def test_needs_manual_apply_when_a_plane_remains(landing, capsys):
    ln, _ = _deployed(landing, Fakes(plane="initial_setup.yml --tags k3s"))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert exc.value.verdict == "needs-manual-apply"
    assert "STILL UNAPPLIED" in capsys.readouterr().out


@pytest.mark.parametrize(
    "state, verdict, code, cause",
    [
        ({"hold_sha": "abc"}, "deploy-failed", 1, "tick-held"),
        ({"behind_since": "x"}, "deferred", 75, ""),
        (APPLIED, "settled", 0, ""),
    ],
)
def test_a_self_applied_half_reads_the_deployers_state(
    landing, state, verdict, code, cause
):
    ln, _ = _deployed(landing, Fakes(self_applied=True, state=state))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (code, verdict)
    assert ln.ledger.cause == cause


def test_a_held_tick_names_the_clear_command_with_the_full_sha(landing, capsys):
    held = "deadbeef" * 5
    ln, _ = _deployed(landing, Fakes(self_applied=True, state={"hold_sha": held}))
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    assert f"gitops_state.py clear-hold {held}`" in capsys.readouterr().out


def test_an_ordinary_service_pr_ignores_the_deployers_state(landing):
    """behind_since is somebody else's pending merge when the tick does not apply this PR."""
    ln, _ = _deployed(landing, Fakes(state={"behind_since": "x"}))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert exc.value.verdict == "settled"


def test_a_self_applied_role_that_reaches_other_hosts_is_not_settled(landing, capsys):
    """The services are live, the tick converged, and two hosts are still owed."""
    ln, _ = _deployed(
        landing,
        Fakes(
            self_applied=True,
            remaining_setup="daniel-server, daniel-pi",
            state=APPLIED,
        ),
    )
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "other hosts still need it" in exc.value.detail
    assert (
        "services deployed, and STILL UNAPPLIED on the hosts this tick never touched: "
        "daniel-server, daniel-pi" in capsys.readouterr().out
    )


def test_no_remaining_hosts_still_settles(landing):
    """The reject half: the tick's host is the only one reached."""
    ln, _ = _deployed(
        landing, Fakes(self_applied=True, remaining_setup="", state=APPLIED)
    )
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "settled")


# ── converged is not applied ────────────────────────────────────────────────


def test_a_converged_tick_that_recorded_no_apply_is_not_settled(landing, capsys):
    """Something else fast-forwarded the checkout, so the tick applied
    nothing and will never see the range again — and every marker land.sh reads is in
    the settled state."""
    ln, _ = _deployed(landing, Fakes(self_applied=True, state={}))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    out = capsys.readouterr().out
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "recorded no broad apply covering this PR" in out
    assert "never see this range again" in out
    assert "`ansible-playbook ansible/initial_setup.yml --tags x`" in out


def test_a_tick_still_behind_origin_is_not_told_it_will_never_return(landing, capsys):
    """A tick that landed at a green ancestor has a range left to cross, so say so.

    It crossed this PR's merge commit — which is why the landing is not BEHIND — but it is
    still behind the tip, and a later tick can still record the apply. The stranded wording
    of the test above is false here.
    """
    ln, _ = _deployed(
        landing,
        Fakes(
            self_applied=True, state={"behind_since": "abc123 1.0"}, merge_applied_rc=0
        ),
    )
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    out = capsys.readouterr().out
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "a later tick may yet apply this range" in out
    assert "never see this range again" not in out


def test_an_apply_recorded_at_a_commit_without_this_pr_is_not_settled(landing):
    """A receipt exists but for an EARLIER range — coverage, not presence, decides."""
    ln, _ = _deployed(
        landing,
        Fakes(
            self_applied=True,
            state=receipt({"ansible/deploy.yml": []}),
            is_ancestor_rc=1,
        ),
    )
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")


# The tick's own deferral of a deploy plane it could not narrow (#4333): the range is
# merged, its receipt applies nothing and records the owed services under `deferred`, and
# each of them holds a `k8s_unapplied` line until a deploy or the render proof covers it.
OWED = ("sonarr", "radarr")
DEFERRED_RECEIPT = receipt({}, deferred={"ansible/deploy.yml": list(OWED)})
OWED_LINES = {
    MARKERS["owed"]: "\n".join(
        owed_line(OWED_K8S_UNAPPLIED, s, "d" * 40, 1.0) for s in OWED
    )
}
# The remedy `land_tags.broad_remediation` derives for a deploy-plane path.
DEPLOY_PLANE_COMMAND = (
    "`git merge --ff-only origin/master` FIRST, then "
    "`uv run ansible-playbook ansible/deploy.yml`"
)
DEPLOY_PLANE_PATH = "ansible/filter_plugins/toposort.py"


def _deferred(landing, state: dict, pr_path: str = DEPLOY_PLANE_PATH):
    """A converged tick, the deployer's markers `state`, for a PR changing `pr_path`."""
    ln, _ = _deployed(
        landing,
        Fakes(
            self_applied=True, self_applied_command=DEPLOY_PLANE_COMMAND, state=state
        ),
    )
    ln.classification = replace(ln.classification, pr_paths=(pr_path,))
    return ln


def test_a_deploy_plane_the_tick_deferred_prints_the_deferral_not_deploy_yml(
    landing, capsys
):
    """#4843: the ledger lines are the remedy, not a whole deploy.yml after an ff-merge."""
    ln = _deferred(landing, DEFERRED_RECEIPT | OWED_LINES)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    out = capsys.readouterr().out
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "k8s_unapplied ledger: 2 of its services are still owed" in out
    assert "probe.py gitops-state" in out
    assert "Apply it:" not in out
    assert "ff-only" not in out
    assert "never see this range again" not in out


def test_a_deferred_plane_whose_services_all_discharged_is_settled(landing):
    """The receipt outlives the lines, so a re-run after the render proof reads applied."""
    ln = _deferred(landing, DEFERRED_RECEIPT)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "settled")


def test_a_deferred_plane_that_owed_no_service_is_not_settled(landing, capsys):
    """#4865: an empty set counted 0 owed and read as discharged, for a plane nothing ran.

    A deployer before #4865 wrote one when host_vars was unreadable, so the remedy is the
    stranded-range note and the deploy.yml command, not the ledger lines.
    """
    ln = _deferred(landing, receipt({}, deferred={"ansible/deploy.yml": []}))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    out = capsys.readouterr().out
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert f"Apply it: {DEPLOY_PLANE_COMMAND}" in out
    assert "k8s_unapplied ledger" not in out


def test_a_deferred_plane_with_an_unreadable_ledger_still_names_the_deferral(
    landing, capsys
):
    """An unread count proves nothing either way: not settled, and not stranded either."""
    ln = _deferred(landing, DEFERRED_RECEIPT)
    ln.tools.read_state = lambda root, name: (
        None if name == MARKERS["owed"] else DEFERRED_RECEIPT.get(name, "")
    )
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    out = capsys.readouterr().out
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "an unknown number of its services are still owed" in out
    assert "Apply it:" not in out


@pytest.mark.parametrize(
    "state, pr_path",
    [
        # Another session's ff-merge crossed the PR, so no receipt; a later tick owes
        # unrelated services at origins past the merge. That is not this PR's deferral.
        (OWED_LINES, DEPLOY_PLANE_PATH),
        # A setup-plane PR owes `initial_setup.yml`, whatever its range's receipt deferred.
        (
            DEFERRED_RECEIPT | OWED_LINES,
            "ansible/roles/setup/renovate_agent/tasks/main.yml",
        ),
    ],
    ids=["no-receipt-later-lines", "setup-plane-pr"],
)
def test_a_range_the_tick_did_not_defer_keeps_the_stranded_remedy(
    landing, capsys, state, pr_path
):
    """Only the receipt of the range that crossed the merge names this PR's deferral."""
    ln = _deferred(landing, state, pr_path)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    out = capsys.readouterr().out
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "never see this range again" in out
    assert f"Apply it: {DEPLOY_PLANE_COMMAND}" in out
    assert "k8s_unapplied" not in out


def test_an_unrecorded_apply_read_mid_tick_is_deferred_not_owed_to_a_hand(
    landing, capsys
):
    """A tick that already ff-merged this PR answers CONVERGED, never BEHIND, so the
    abandoned-watch arm above cannot catch it — and the receipt's `applied` is written only
    once the apply returns."""
    ln, _ = _deployed(landing, Fakes(self_applied=True, state={}, merge_applied_rc=0))
    ln.tick_watch_abandoned = True
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "deferred")
    assert "stopped watching a tick that was still applying" in capsys.readouterr().out


def test_an_ordinary_service_pr_never_asks_about_a_broad_apply(landing):
    """The receipt speaks to a landing only when the tick applies part of THIS PR."""
    ln, _ = _deployed(landing, Fakes(self_applied=False, state={}))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "settled")


def test_the_gate_renders_the_tree_that_was_deployed(landing):
    """`probe.py health` enumerates workloads by rendering the role's manifests from its cwd.

    A landing that deployed a snapshot of its merge commit has not moved the primary checkout,
    so a role that commit ADDS enumerates nothing there and the whole gate reads `skipped` --
    green, on the first landing of a new service.
    """
    ln, calls = _deployed(landing)
    ln.deployed_at = MERGE_SHA
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    assert next(c for c in calls if c[0] == "snapshot")[1][1] == MERGE_SHA
    assert next(c for c in calls if c[0] == "gate")[2] == {"cwd": Path("/snap")}


def test_a_deploy_from_the_primary_gates_the_primary(landing):
    """CLEAN half: the fallback path keeps the gate call it has always made."""
    ln, calls = _deployed(landing)
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    assert "snapshot" not in [c[0] for c in calls]
    assert next(c for c in calls if c[0] == "gate")[2] == {"cwd": None}


def test_a_snapshot_that_could_not_be_taken_fails_the_gate(landing, capsys):
    """FLAGGED half: no tree that can answer, so no verdict -- never a green one.

    Degrading to the primary is what the first cut did, and the primary is the one tree that
    cannot see a role the deployed commit ADDS: `check_one` returns `skipped` and the landing
    reads `settled` having gated nothing. That is the whole gap this path exists to close, so
    here it fails closed instead.
    """
    ln, _calls = _deployed(landing, Fakes(gate_snapshot=None, merge_applied_rc=1))
    ln.deployed_at = MERGE_SHA
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert exc.value.verdict == "unhealthy"
    assert "does not carry it yet" in capsys.readouterr().out


def test_a_snapshot_that_failed_over_a_primary_that_has_the_commit_gates_there(
    landing, capsys
):
    """CLEAN half: an equivalent-or-newer primary answers the same question, so it may.

    Without this arm a tick that converged the checkout a second before the gate would turn a
    healthy landing unhealthy over a worktree that was never needed.
    """
    ln, calls = _deployed(landing, Fakes(gate_snapshot=None, merge_applied_rc=0))
    ln.deployed_at = MERGE_SHA
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    # The primary itself, not land.sh's own checkout, which may predate the commit (#5744).
    assert next(c for c in calls if c[0] == "gate")[2] == {"cwd": ln.opts.primary}
    assert "already carries it" in capsys.readouterr().out


def test_an_unreadable_deployer_state_is_not_settled(landing, capsys):
    """Finding 13's rejecting half at step 6: services live, the tick's own half unknown."""
    ln, _ = _deployed(landing, Fakes(self_applied=True))
    ln.tools.read_state = lambda root, name: None
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "could not be read" in capsys.readouterr().out


def test_a_failed_snapshot_asks_whether_the_primary_carries_the_retargeted_commit(
    landing,
):
    """After a retarget, `deployed_at` is newer than the merge commit, and a primary holding
    only the merge commit is not an equivalent tree to gate from."""
    ln, calls = _deployed(landing, Fakes(gate_snapshot=None, merge_applied_rc=1))
    ln.deployed_at = "d" * 40
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    asked = [c[1] for c in calls if c[0] == "git" and c[1][:1] == ("merge-base",)]
    assert ("merge-base", "--is-ancestor", "d" * 40, "HEAD") in asked
