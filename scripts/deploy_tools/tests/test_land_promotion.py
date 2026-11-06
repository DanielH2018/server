"""A k8s tag lands through `promote`: the split before deploy.sh and the wait.

The plan is docs/target-architecture/deploy/d2-first-leaf.md, *How `land.sh` waits*. Every
boundary is a fake from `_land_fakes.py`; `promote.land_wait` itself is tested in
test_promote_land_wait.py.

Run: uv run pytest scripts/deploy_tools/tests/test_land_promotion.py
"""

import pytest
from dataclasses import replace

from _land_fakes import MERGE_SHA, Fakes, receipt
from deploy_tools.land_lib import promotion
from deploy_tools.land_lib.outcome import PROMOTE_TIMEOUT_NOTE
from deploy_tools.land_lib.host_wait import HostTarget
from deploy_tools.promote_lib.wait import WaitResult

HEAD = "c" * 40
BASE = "prbase"  # what the fake `git merge-base` answers


def _names(calls):
    return [c[0] for c in calls]


# What land-wait answers when the changed set names bento-pdf.
SETTLED_BENTO = WaitResult("settled", detail="1 applied", tags=("bento-pdf",))


def _fakes(**kwargs: object) -> Fakes:
    """bento-pdf is flux-owned at the merge commit, and the PR reaches it alone."""
    fakes = Fakes(
        derived=(["bento-pdf"], "pr"),
        owners={MERGE_SHA: {"bento-pdf": "flux"}},
        land_wait=[SETTLED_BENTO],
    )
    return replace(fakes, **kwargs)


def test_a_pr_whose_only_tag_is_flux_owned_never_reaches_deploy_sh(land_run):
    rc, out, _, calls, logline = land_run([], _fakes())
    assert rc == 0 and "VERDICT: settled" in out
    # The path-derived tag is not handed over: the changed set names it (D6.12).
    assert [c[1][:2] for c in calls if c[0] == "land_wait"] == [(MERGE_SHA, [])]
    # The gate runs on the changed set's tag, from the merge commit's snapshot.
    assert [c[1] for c in calls if c[0] == "gate"] == [(["bento-pdf"],)]
    assert "tags=bento-pdf" in logline


def test_a_tags_override_hands_its_flux_owned_tag_to_land_wait(land_run):
    """`--tags` is the operator's list, not a path derivation, so it reaches the wait."""
    _, _, _, calls, _ = land_run(["--tags", "bento-pdf"], _fakes())
    assert [c[1][:2] for c in calls if c[0] == "land_wait"] == [
        (MERGE_SHA, ["bento-pdf"])
    ]


def test_a_changed_set_name_flux_does_not_own_is_not_gated(land_run):
    """A widened set names every Kustomization; only an `owner: flux` tag reaches the gate."""
    widened = replace(SETTLED_BENTO, tags=("bento-pdf", "flux-bootstrap"))
    _, _, _, calls, _ = land_run([], _fakes(land_wait=[widened]))
    assert [c[1] for c in calls if c[0] == "gate"] == [(["bento-pdf"],)]


@pytest.mark.parametrize(
    "path",
    [
        "docs/landing.md",
        "ansible/roles/k8s/sonarr/CLAUDE.md",
        "scripts/lib/tests/test_x.py",
    ],
)
def test_a_pr_of_documents_and_tests_alone_waits_on_no_promotion(land_run, path):
    fakes = Fakes(
        gh_views={"files,changedFiles": {"files": [{"path": path}], "changedFiles": 1}},
        derived=([], "pr"),
    )
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 0 and "VERDICT: nothing-to-deploy" in out
    assert "land_wait" not in _names(calls)


def test_a_held_promotion_is_deploy_failed_with_promote_held(land_run):
    held = WaitResult("deploy-failed", "promote-held", "DEPLOY_HOLD holds abc")
    rc, out, _, calls, logline = land_run([], _fakes(land_wait=[held]))
    assert rc == 1 and "VERDICT: deploy-failed" in out
    assert "DEPLOY_HOLD holds abc" in out
    assert "cause=promote-held" in logline
    assert "gate" not in _names(calls)


def test_a_held_promotion_records_the_changed_sets_tags_on_the_ledger(land_run):
    held = WaitResult(
        "deploy-failed", "promote-held", "DEPLOY_HOLD holds abc", tags=("bento-pdf",)
    )
    rc, _, _, _, logline = land_run([], _fakes(land_wait=[held]))
    assert rc == 1
    assert "tags=bento-pdf" in logline


def test_a_held_promotion_still_names_the_plane_and_the_host_it_carries(land_run):
    """The wait ends the landing before the health step and the host wait print these."""
    held = WaitResult("deploy-failed", "promote-held", "DEPLOY_HOLD holds abc")
    fakes = _fakes(
        land_wait=[held],
        gh_views={
            "files,changedFiles": {
                "files": [{"path": "hosts/daniel-pi/package.yml"}],
                "changedFiles": 1,
            }
        },
        plane="rotate it by hand",
        host_targets=[HostTarget("daniel-pi", armed=True)],
    )
    rc, out, _, _, _ = land_run([], fakes)
    assert rc == 1 and "VERDICT: deploy-failed" in out
    assert "it needs applying by hand: rotate it by hand" in out
    assert "it also changes the package of daniel-pi" in out


def test_a_failed_soak_is_deploy_failed_with_soak_failed(land_run):
    """The red half is the mapping before D5.3b3, which labelled every `deploy-failed`
    `promote-held`."""
    failed = WaitResult(
        "deploy-failed", "soak-failed", "flux/soak reads failure", revision="d" * 40
    )
    rc, out, _, calls, logline = land_run([], _fakes(land_wait=[failed]))
    assert rc == 1 and "VERDICT: deploy-failed" in out
    assert "flux/soak reads failure" in out and "soak failed on dddddddddddd" in out
    assert "cause=soak-failed" in logline and "promote-held" not in logline
    assert "gate" not in _names(calls)


def test_a_promotion_timeout_is_deferred_with_promote_timeout(land_run):
    timed_out = WaitResult("gave-up", "promote-timeout", "no promotion covered it")
    rc, out, _, _, logline = land_run([], _fakes(land_wait=[timed_out]))
    assert rc == 75 and "VERDICT: deferred" in out
    assert "cause=promote-timeout" in logline
    # The note `land_probe` reads to report this deferral as `gave-up`, a resume point.
    assert PROMOTE_TIMEOUT_NOTE in out


def test_a_suspended_tag_is_nothing_to_deploy_without_a_gate(land_run):
    suspended = WaitResult("suspended", "promote-suspended", "bento-pdf is suspended")
    rc, out, _, calls, _ = land_run([], _fakes(land_wait=[suspended]))
    assert rc == 0 and "VERDICT: nothing-to-deploy" in out
    assert "bento-pdf is suspended" in out
    assert "gate" not in _names(calls)


@pytest.mark.parametrize(
    "owed,line",
    [
        (
            {"plane": "rotate it by hand"},
            "it needs applying by hand: rotate it by hand",
        ),
        ({"remaining_setup": "`cmd-server`"}, "`cmd-server`"),
    ],
)
def test_a_suspended_tag_with_a_plane_owed_is_needs_manual_apply(land_run, owed, line):
    suspended = WaitResult("suspended", "promote-suspended", "bento-pdf is suspended")
    fakes = _fakes(
        land_wait=[suspended],
        gh_views={
            "files,changedFiles": {
                "files": [{"path": "hosts/daniel-pi/package.yml"}],
                "changedFiles": 1,
            }
        },
        host_targets=[HostTarget("daniel-pi", armed=True)],
        **owed,
    )
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 1 and "VERDICT: needs-manual-apply" in out
    assert line in out
    assert "it also changes the package of daniel-pi" in out
    assert "gate" not in _names(calls)


def test_a_suspended_tag_with_a_host_package_still_waits_for_the_host(land_run):
    suspended = WaitResult(
        "suspended",
        "promote-suspended",
        "bento-pdf is suspended",
        suspended=("bento-pdf",),
        tags=("bento-pdf",),
    )
    fakes = _fakes(
        land_wait=[suspended],
        gh_views={
            "files,changedFiles": {
                "files": [{"path": "hosts/daniel-pi/package.yml"}],
                "changedFiles": 1,
            }
        },
        host_targets=[HostTarget("daniel-pi", armed=True)],
    )
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 0 and "VERDICT: settled" in out
    assert "hosts: daniel-pi" in out
    assert "did not wait" not in out
    assert "wait_for_hosts" in _names(calls)
    assert [c[1] for c in calls if c[0] == "gate"] == [([],)]


# A wait whose changed set names the suspended tag reaches the health step, and one whose
# changed set is empty reaches `no_tag_outcome`. Each grades the tick's half on its own.
_SUSPENDED_WAITS = [
    pytest.param(("bento-pdf",), id="health-step"),
    pytest.param((), id="no-tag-outcome"),
]


def _tick_half_under_a_suspended_wait(changed, state) -> Fakes:
    """A PR the tick applies part of, with no plane, no host target and every tag suspended."""
    suspended = WaitResult(
        "suspended",
        "promote-suspended",
        "bento-pdf is suspended",
        suspended=("bento-pdf",),
        tags=changed,
    )
    return _fakes(land_wait=[suspended], self_applied=True, state=state)


@pytest.mark.parametrize("changed", _SUSPENDED_WAITS)
def test_a_suspended_wait_with_a_held_tick_half_is_deploy_failed(land_run, changed):
    fakes = _tick_half_under_a_suspended_wait(changed, {"hold_sha": "deadbeef"})
    rc, out, _, calls, logline = land_run([], fakes)
    assert rc == 1 and "VERDICT: deploy-failed" in out
    assert "cause=tick-held" in logline
    assert "gitops_state.py clear-hold deadbeef" in out
    assert not [c for c in calls if c[0] == "gate" and "bento-pdf" in c[1][0]]


@pytest.mark.parametrize("changed", _SUSPENDED_WAITS)
@pytest.mark.parametrize(
    "state,expected_rc,verdict",
    [
        pytest.param({"behind_since": "x"}, 75, "deferred", id="behind"),
        pytest.param({}, 1, "needs-manual-apply", id="no-recorded-apply"),
        pytest.param({"hold_sha": None}, 1, "needs-manual-apply", id="unreadable"),
    ],
)
def test_a_suspended_wait_grades_the_tick_half_into_a_verdict(
    land_run, changed, state, expected_rc, verdict
):
    fakes = _tick_half_under_a_suspended_wait(changed, state)
    rc, out, _, _, _ = land_run([], fakes)
    assert rc == expected_rc and f"VERDICT: {verdict}" in out


@pytest.mark.parametrize("changed", _SUSPENDED_WAITS)
def test_a_suspended_wait_with_an_applied_tick_half_settles(land_run, changed):
    """Nothing is owed, and the tick did apply a half of this PR, so it is not a no-op."""
    applied = receipt({"ansible/initial_setup.yml": ["renovate_agent"]})
    fakes = _tick_half_under_a_suspended_wait(changed, applied)
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 0 and "VERDICT: settled" in out
    assert not [c for c in calls if c[0] == "gate" and "bento-pdf" in c[1][0]]


def test_a_settled_wait_that_names_a_suspended_tag_gates_the_rest(land_run):
    settled = WaitResult(
        "settled",
        detail="3 applied",
        suspended=("bento-pdf",),
        tags=("bento-pdf", "texbrain"),
    )
    fakes = _fakes(
        derived=(["bento-pdf", "texbrain"], "pr"),
        owners={MERGE_SHA: {"bento-pdf": "flux", "texbrain": "flux"}},
        land_wait=[settled],
    )
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 0 and "VERDICT: settled" in out
    assert [c[1] for c in calls if c[0] == "gate"] == [(["texbrain"],)]


def test_an_unreadable_ownership_map_after_the_wait_gates_every_changed_name(land_run):
    """Asked too much, a gate fails loudly; asked too little, it passes over a broken tag."""
    fakes = _fakes(
        derived=([], "pr"),
        owners={MERGE_SHA: ValueError("owner 'flx' is not one of ...")},
    )
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 0 and "gating every changed name" in out
    assert [c[1] for c in calls if c[0] == "gate"] == [(["bento-pdf"],)]


def test_an_unreadable_k8s_entry_list_deploys_nothing(land_run):
    fakes = _fakes(k8s_tags={MERGE_SHA: ValueError("host_vars does not parse")})
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 1 and "VERDICT: refused" in out
    assert "could not read which tags Flux applies" in out
    assert "k8s_tags_at" in _names(calls)
    assert "land_wait" not in _names(calls)


def test_a_landing_deadline_caps_the_promotion_wait(land_run, monkeypatch):
    """`LAND_DEADLINE` less what the landing spent and the reserve for the gate (#5199)."""
    monkeypatch.setenv("LAND_DEADLINE", "5400")
    _, _, _, calls, _ = land_run([], _fakes())
    (cap,) = [c[1][3] for c in calls if c[0] == "land_wait"]
    assert cap is not None and 0 < cap <= 5400 - promotion.DEADLINE_RESERVE_S


def test_without_a_landing_deadline_the_promotion_wait_is_uncapped(land_run):
    _, _, _, calls, _ = land_run([], _fakes())
    assert [c[1][3:] for c in calls if c[0] == "land_wait"] == [(None, 0)]


def test_a_landers_window_grace_reaches_the_promotion_wait(land_run, monkeypatch):
    """`LAND_WINDOW_GRACE` is what the reboot window may add to the cap (#5457); unset is 0."""
    monkeypatch.setenv("LAND_WINDOW_GRACE", "22200")
    _, _, _, calls, _ = land_run([], _fakes())
    assert [c[1][4] for c in calls if c[0] == "land_wait"] == [22200]


def test_a_tag_no_k8s_entry_declares_is_flagged(land_run):
    """Nothing applies it, so the landing must not read the promotion as covering it."""
    fakes = Fakes(k8s_tags={MERGE_SHA: {"sonarr"}})
    rc, out, _, calls, logline = land_run(["--tags", "sonarr,typo"], fakes)
    assert rc == 1 and "VERDICT: deploy-failed" in out and "typo" in out
    assert "cause=tag-miss" in logline and "tags=typo" in logline
    assert not {"land_wait", "gate"} & set(_names(calls))


def test_a_tag_a_k8s_entry_declares_is_clean(land_run):
    fakes = Fakes(k8s_tags={MERGE_SHA: {"sonarr"}})
    rc, out, _, calls, logline = land_run(["--tags", "sonarr"], fakes)
    assert rc == 0 and "VERDICT: settled" in out
    assert "cause=tag-miss" not in logline
    assert "land_wait" in _names(calls)
