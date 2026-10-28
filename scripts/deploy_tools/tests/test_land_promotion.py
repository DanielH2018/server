"""A tag Flux applies lands through `promote`: the split before deploy.sh, the wait, the handback.

The plan is docs/target-architecture/deploy/d2-first-leaf.md, *How `land.sh` waits*. Every
boundary is a fake from `_land_fakes.py`; `promote.land_wait` itself is tested in
test_promote_land_wait.py.

Run: uv run pytest scripts/deploy_tools/tests/test_land_promotion.py
"""

import subprocess

import pytest
from dataclasses import replace

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import deploy, land_flux, merge, promotion
from deploy_tools.land_lib.outcome import PROMOTE_TIMEOUT_NOTE, Outcome
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
    assert "deploy" not in _names(calls)
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


def test_a_pr_with_an_ansible_owned_tag_too_deploys_that_tag_alone(land_run):
    rc, out, _, calls, _ = land_run([], _fakes(derived=(["sonarr", "bento-pdf"], "pr")))
    assert rc == 0 and "VERDICT: settled" in out
    assert [c[1][1] for c in calls if c[0] == "deploy"] == [["sonarr"]]
    assert _names(calls).index("deploy") < _names(calls).index("land_wait")
    assert [c[1] for c in calls if c[0] == "gate"] == [(["sonarr", "bento-pdf"],)]


def test_an_ansible_owned_pr_deploys_and_then_waits_on_promotion(land_run):
    """Every landing whose paths may move the bundle waits on land-wait (D6.12)."""
    rc, out, _, calls, _ = land_run([], Fakes())
    assert rc == 0 and "VERDICT: settled" in out
    assert [c[1][1] for c in calls if c[0] == "deploy"] == [["sonarr"]]
    assert [c[1][:2] for c in calls if c[0] == "land_wait"] == [(MERGE_SHA, [])]
    assert _names(calls).index("deploy") < _names(calls).index("land_wait")


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


def test_a_released_tag_waits_but_is_neither_published_nor_gated(land_run):
    """`owner: released` is mid-handback: no Ansible path applies it and Flux no longer
    publishes it, so the wait covers the prune and the gate has nothing of it to grade."""
    fakes = _fakes(owners={MERGE_SHA: {"bento-pdf": "released"}})
    rc, _, _, calls, _ = land_run([], fakes)
    assert rc == 0
    assert "deploy" not in _names(calls)
    assert [c[1][:2] for c in calls if c[0] == "land_wait"] == [(MERGE_SHA, [])]
    assert "gate" not in _names(calls)


def test_a_held_promotion_is_deploy_failed_with_promote_held(land_run):
    held = WaitResult("deploy-failed", "promote-held", "DEPLOY_HOLD holds abc")
    rc, out, _, calls, logline = land_run([], _fakes(land_wait=[held]))
    assert rc == 1 and "VERDICT: deploy-failed" in out
    assert "DEPLOY_HOLD holds abc" in out
    assert "cause=promote-held" in logline
    assert "gate" not in _names(calls)


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


def test_a_suspended_tag_beside_an_ansible_owned_one_still_gates_that_one(land_run):
    suspended = WaitResult(
        "suspended",
        "promote-suspended",
        "bento-pdf is suspended",
        suspended=("bento-pdf",),
    )
    fakes = _fakes(derived=(["sonarr", "bento-pdf"], "pr"), land_wait=[suspended])
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 0 and "VERDICT: settled" in out
    assert [c[1] for c in calls if c[0] == "gate"] == [(["sonarr"],)]


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


def test_an_unreadable_ownership_map_deploys_nothing(land_run):
    fakes = _fakes(owners={MERGE_SHA: ValueError("owner 'flx' is not one of ...")})
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 1 and "VERDICT: refused" in out
    assert "deploy" not in _names(calls) and "land_wait" not in _names(calls)


# -- the handback refusal --------------------------------------------------------------------


def _handback(landing, kustomizations):
    """A PR whose head removes `owner: released` from bento-pdf, green and left to merge."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": [
                    {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": HEAD},
                    {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": HEAD},
                ]
            },
            owners={BASE: {"bento-pdf": "released"}, HEAD: {}},
            kustomizations=kustomizations,
        )
    )
    ln.direct_merge_subject = "Hand bento-pdf back to Ansible"
    return ln, calls


def test_a_handback_is_refused_while_its_kustomization_exists(landing):
    ln, calls = _handback(landing, {"bento-pdf"})
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.rc == 1
    assert "still exists in flux-system" in exc.value.error
    assert [c[1] for c in calls if c[0] == "kustomization_exists"] == [("bento-pdf",)]
    assert not [c for c in calls if c[0] == "gh"]


def test_a_handback_merges_once_its_kustomization_is_gone(landing):
    ln, calls = _handback(landing, set())
    merge.await_merge(ln)
    assert [c[1][3] for c in calls if c[0] == "gh"] == [
        "repos/{owner}/{repo}/pulls/999/merge"
    ]


def test_an_unreadable_kustomization_refuses_the_handback(landing):
    ln, calls = _handback(landing, RuntimeError("connection refused"))
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert "could not be read" in exc.value.error
    assert not [c for c in calls if c[0] == "gh"]


def test_a_pr_that_hands_nothing_back_reads_no_kustomization(landing):
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": [
                    {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": HEAD},
                    {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": HEAD},
                ]
            },
            owners={BASE: {"bento-pdf": "flux"}, HEAD: {"bento-pdf": "flux"}},
        )
    )
    ln.direct_merge_subject = "Bump bento-pdf"
    merge.await_merge(ln)
    assert "kustomization_exists" not in _names(calls)
    assert [c[0] for c in calls if c[0] == "gh"] == ["gh"]


def test_a_handback_refused_through_land_sh_never_reaches_deploy_sh(land_run):
    """The whole landing: --arm-merge --await-merge stops before the merge call."""
    fakes = Fakes(
        gh_views={
            "state,title,body,reviewDecision": {
                "state": "OPEN",
                "title": "Hand bento-pdf back to Ansible",
                "body": "",
                "reviewDecision": "",
            },
            "state,mergeable,headRefOid": {
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "headRefOid": HEAD,
            },
        },
        owners={BASE: {"bento-pdf": "released"}, HEAD: {}},
        kustomizations={"bento-pdf"},
    )
    rc, out, _, calls, _ = land_run(["--arm-merge", "--await-merge"], fakes)
    assert rc == 1 and "VERDICT: refused" in out
    assert "deploy" not in _names(calls)
    assert not [c for c in calls if c[0] == "gh"]


def test_the_deploy_end_is_stamped_before_the_promotion_wait(landing):
    """The re-gate reads a deploy started after this stamp as a later one, so a stamp taken
    after a 24-minute wait would hide another session's redeploy of the Ansible-owned tag."""
    ln, calls = landing(_fakes(derived=(["sonarr", "bento-pdf"], "pr")))
    ln.classification = replace(
        ln.classification, pr_paths=("ansible/roles/k8s/sonarr/defaults/main.yml",)
    )
    ln.merge_sha = MERGE_SHA
    ln.resolved_tags = ["sonarr", "bento-pdf"]
    ln.tools.wall_clock = lambda: calls.append(("wall_clock", (), {})) or 5.0
    deploy.deploy_phase(ln)
    names = _names(calls)
    assert names.index("deploy") < names.index("wall_clock") < names.index("land_wait")


# -- the Kustomization read ------------------------------------------------------------------


def _kubectl_answering(rc: int, stderr: str = ""):
    """A `lib.kubectl.kubectl` that answers `rc` and `stderr` for every read."""

    def kubectl(cluster, *args, timeout):
        return subprocess.CompletedProcess(args, rc, "", stderr)

    return kubectl


def test_a_kustomization_kubectl_lists_exists():
    assert land_flux.kustomization_exists("bento-pdf", _kubectl_answering(0)) is True


def test_a_not_found_kustomization_does_not_exist():
    stderr = 'Error from server (NotFound): kustomizations "bento-pdf" not found'
    run = _kubectl_answering(1, stderr)
    assert land_flux.kustomization_exists("bento-pdf", run) is False


def test_a_forbidden_kustomization_read_is_unreadable():
    stderr = "Error from server (Forbidden): kustomizations is forbidden"
    run = _kubectl_answering(1, stderr)
    with pytest.raises(land_flux.KustomizationUnreadable, match="Forbidden"):
        land_flux.kustomization_exists("bento-pdf", run)


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
