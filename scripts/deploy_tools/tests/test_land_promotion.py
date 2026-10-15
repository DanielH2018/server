"""A tag Flux applies lands through `promote`: the split before deploy.sh, the wait, the handback.

The plan is docs/target-architecture/deploy/d2-first-leaf.md, *How `land.sh` waits*. Every
boundary is a fake from `_land_fakes.py`; `promote.land_wait` itself is tested in
test_promote_land_wait.py.

Run: uv run pytest scripts/deploy_tools/tests/test_land_promotion.py
"""

import subprocess

import pytest
from dataclasses import replace

from _land_fakes import MERGE_SHA, Fakes, receipt
from deploy_tools.land_lib import deploy, land_flux, merge
from deploy_tools.land_lib.outcome import PROMOTE_TIMEOUT_NOTE, Outcome
from deploy_tools.promote_lib.wait import WaitResult

HEAD = "c" * 40
BASE = "prbase"  # what the fake `git merge-base` answers


def _names(calls):
    return [c[0] for c in calls]


def _fakes(**kwargs: object) -> Fakes:
    """bento-pdf is flux-owned at the merge commit, and the PR reaches it alone."""
    fakes = Fakes(
        derived=(["bento-pdf"], "pr"), owners={MERGE_SHA: {"bento-pdf": "flux"}}
    )
    return replace(fakes, **kwargs)


def test_a_pr_whose_only_tag_is_flux_owned_never_reaches_deploy_sh(land_run):
    rc, out, _, calls, logline = land_run([], _fakes())
    assert rc == 0 and "VERDICT: settled" in out
    assert "deploy" not in _names(calls)
    assert [c[1][:2] for c in calls if c[0] == "land_wait"] == [
        (MERGE_SHA, ["bento-pdf"])
    ]
    # The gate still runs on the published tag, from the merge commit's snapshot.
    assert [c[1] for c in calls if c[0] == "gate"] == [(["bento-pdf"],)]
    assert "tags=bento-pdf" in logline


def test_a_pr_with_an_ansible_owned_tag_too_deploys_that_tag_alone(land_run):
    rc, out, _, calls, _ = land_run([], _fakes(derived=(["sonarr", "bento-pdf"], "pr")))
    assert rc == 0 and "VERDICT: settled" in out
    assert [c[1][1] for c in calls if c[0] == "deploy"] == [["sonarr"]]
    assert _names(calls).index("deploy") < _names(calls).index("land_wait")
    assert [c[1] for c in calls if c[0] == "gate"] == [(["sonarr", "bento-pdf"],)]


def test_an_ansible_owned_pr_never_waits_on_promotion(land_run):
    rc, out, _, calls, _ = land_run([], Fakes())
    assert rc == 0 and "VERDICT: settled" in out
    assert "land_wait" not in _names(calls)
    assert [c[1][1] for c in calls if c[0] == "deploy"] == [["sonarr"]]


def test_a_released_tag_waits_but_is_neither_published_nor_gated(land_run):
    """`owner: released` is mid-handback: no Ansible path applies it and Flux no longer
    publishes it, so the wait covers the prune and the gate has nothing of it to grade.

    The published list handed to land-wait is the one its annotation names, so an empty one
    here is what keeps the tag out of `services=`."""
    fakes = _fakes(owners={MERGE_SHA: {"bento-pdf": "released"}})
    rc, _, _, calls, _ = land_run([], fakes)
    assert rc == 0
    assert "deploy" not in _names(calls)
    assert [c[1][:2] for c in calls if c[0] == "land_wait"] == [(MERGE_SHA, [])]
    assert [c[1] for c in calls if c[0] == "gate"] == [([],)]


def test_a_held_promotion_is_deploy_failed_with_promote_held(land_run):
    held = WaitResult("deploy-failed", "promote-held", "DEPLOY_HOLD holds abc")
    rc, out, _, calls, logline = land_run([], _fakes(land_wait=[held]))
    assert rc == 1 and "VERDICT: deploy-failed" in out
    assert "DEPLOY_HOLD holds abc" in out
    assert "cause=promote-held" in logline
    assert "gate" not in _names(calls)


def test_a_promotion_timeout_is_deferred_with_promote_timeout(land_run):
    timed_out = WaitResult("gave-up", "promote-timeout", "no promotion covered it")
    rc, out, _, _, logline = land_run([], _fakes(land_wait=[timed_out]))
    assert rc == 75 and "VERDICT: deferred" in out
    assert "cause=promote-timeout" in logline
    # The note `land_probe` reads to report this deferral as `gave-up`, a resume point.
    assert PROMOTE_TIMEOUT_NOTE in out


def test_an_unreadable_ownership_map_deploys_nothing(land_run):
    fakes = _fakes(owners={MERGE_SHA: ValueError("owner 'flx' is not one of ...")})
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 1 and "VERDICT: refused" in out
    assert "deploy" not in _names(calls) and "land_wait" not in _names(calls)


# -- a deploy-plane PR the deployer narrows to a tag Flux applies (#5146) ------------------


def _flip(**kwargs: object) -> Fakes:
    """#5143's shape: one `host_vars` edit sets `owner: flux` on bento-pdf, and nothing else.

    The edit is broad, so no tag is derived and the tick applies it; the deployer's narrowing
    names bento-pdf and the tick drops it, leaving nothing applied."""
    fakes = Fakes(
        gh_views={
            "files,changedFiles": {
                "files": [{"path": "ansible/inventory/host_vars/daniel-box.yml"}],
                "changedFiles": 1,
            }
        },
        derived=([], "pr"),
        self_applied=True,
        narrowed="bento-pdf",
        narrowed_rc=0,
        owners={MERGE_SHA: {"bento-pdf": "flux"}},
        state=receipt({"ansible/deploy.yml": ["narrowed-to-nothing"]}),
        gate=(True, ["bento-pdf: healthy"]),
    )
    return replace(fakes, **kwargs)


def test_an_inventory_flip_to_flux_waits_on_promotion(land_run):
    rc, out, _, calls, _ = land_run([], _flip())
    assert rc == 0 and "VERDICT: settled" in out
    # Narrowed over the PR's own range, not `--since`.
    assert ("narrow", "prbase", "prhead") in [
        c[1] for c in calls if c[0] == "deploy_tags"
    ]
    assert [c[1][:2] for c in calls if c[0] == "land_wait"] == [
        (MERGE_SHA, ["bento-pdf"])
    ]
    assert "deploy" not in _names(calls)
    assert [c[1] for c in calls if c[0] == "gate"] == [(["bento-pdf"],)]


def test_an_inventory_flip_does_not_settle_before_the_promotion_verdict(land_run):
    timed_out = WaitResult("gave-up", "promote-timeout", "no promotion covered it")
    rc, out, _, _, _ = land_run([], _flip(land_wait=[timed_out]))
    assert rc == 75 and "VERDICT: deferred" in out
    assert "VERDICT: settled" not in out


def test_a_narrowed_ansible_owned_tag_settles_on_the_tick(land_run):
    rc, out, _, calls, _ = land_run([], _flip(owners={MERGE_SHA: {}}))
    assert rc == 0 and "VERDICT: settled" in out
    assert "land_wait" not in _names(calls) and "deploy" not in _names(calls)


def test_a_narrowing_that_fails_settles_on_the_tick_as_before(land_run):
    rc, out, _, calls, _ = land_run([], _flip(narrowed="", narrowed_rc=1))
    assert rc == 0 and "VERDICT: settled" in out
    assert "land_wait" not in _names(calls) and "owners_at" not in _names(calls)


def test_an_unreadable_ownership_map_after_the_tick_is_not_a_refusal(land_run):
    """The tick already applied the deploy-plane change, so the landing must not claim that
    nothing was deployed; only whether a promotion owes the tag is unknown."""
    fakes = _flip(owners={MERGE_SHA: ValueError("owner 'flx' is not one of ...")})
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 1 and "VERDICT: needs-manual-apply" in out
    assert "nothing was deployed" not in out
    assert f"promote.py land-wait {MERGE_SHA}" in out
    assert "land_wait" not in _names(calls) and "deploy" not in _names(calls)


def test_a_refused_bulk_flip_still_waits_on_the_tags_it_reached(land_run):
    """`narrow` refuses a list covering most of the fleet, as D5's bulk move would, and still
    prints the tags it reached."""
    rc, _, _, calls, _ = land_run([], _flip(narrowed_rc=3))
    assert rc == 0
    assert [c[1][:2] for c in calls if c[0] == "land_wait"] == [
        (MERGE_SHA, ["bento-pdf"])
    ]


def test_a_service_pr_that_also_flips_a_tag_waits_on_that_tag(land_run):
    fakes = _flip(
        derived=(["sonarr"], "pr"),
        gh_views={
            "files,changedFiles": {
                "files": [
                    {"path": "ansible/roles/k8s/sonarr/defaults/main.yml"},
                    {"path": "ansible/inventory/host_vars/daniel-box.yml"},
                ],
                "changedFiles": 2,
            }
        },
    )
    rc, _, _, calls, _ = land_run([], fakes)
    assert rc == 0
    assert [c[1][1] for c in calls if c[0] == "deploy"] == [["sonarr"]]
    assert [c[1][:2] for c in calls if c[0] == "land_wait"] == [
        (MERGE_SHA, ["bento-pdf"])
    ]


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
