"""H3.9b: the host wait inside the landing -- the candidate at resolve, the render after CI,
the routes through `deploy_phase`, the step before the verdict and the verdict wording.

`host_wait`'s own behaviour (targets, transport, settle rule, deadline) is
`test_land_host_wait.py`. Here `pipeline.host_wait` is a fake (`install_host_wait`), so no
test renders a package or reaches Prometheus.

Run: uv run pytest scripts/deploy_tools/tests/test_land_host_wait_wiring.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import deploy, health_verdict
from deploy_tools.land_lib.host_wait import HostTarget
from deploy_tools.land_lib.landing import Classification
from deploy_tools.land_lib.outcome import Outcome

PI = HostTarget("daniel-pi", armed=True)
PI_PACKAGE = "hosts/daniel-pi/package.yml"


def _files(*paths: str) -> dict:
    return {
        "files,changedFiles": {
            "files": [{"path": p} for p in paths],
            "changedFiles": len(paths),
        }
    }


def _hosts_only(**kw) -> Fakes:
    """A PR that changes only the Pi's package: no tag, no plane, nothing self-applied."""
    return Fakes(gh_views=_files(PI_PACKAGE), derived=([], "pr"), **kw)


def _names(calls):
    return [c[0] for c in calls]


# -- the candidate at resolve --------------------------------------------------------------


def test_a_host_input_path_makes_the_pr_a_candidate_without_running_a_renderer(
    land_run,
):
    rc, out, _, calls, _ = land_run([], _hosts_only(host_targets=[PI]))
    assert rc == 0 and "VERDICT: settled" in out
    # The render is a step-3 call: nothing ran it while the merge commit's CI was unread.
    assert _names(calls).index("host_targets") > _names(calls).index("await_ci")


def test_a_pr_touching_no_host_input_is_not_a_candidate(land_run):
    fakes = Fakes(
        gh_views=_files("docs/landing.md"), derived=([], "pr"), host_targets=[PI]
    )
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 0 and "VERDICT: nothing-to-deploy" in out
    assert "await_ci" not in _names(calls)
    assert "host_targets" not in _names(calls)


def test_a_hosts_only_pr_does_not_finish_before_ci(land_run):
    """The shortcut treats a candidate like a tag: `nothing-to-deploy` waits for the render."""
    _, out, _, calls, _ = land_run([], _hosts_only(await_ci=[(1, "red")]))
    assert "nothing-to-deploy" not in out
    assert "await_ci" in _names(calls)


# -- the render after CI -------------------------------------------------------------------


def test_paths_that_only_looked_like_host_inputs_end_nothing_to_deploy(land_run):
    rc, out, _, calls, _ = land_run([], _hosts_only(host_targets=[]))
    assert rc == 0 and "VERDICT: nothing-to-deploy" in out
    assert _names(calls).index("host_targets") > _names(calls).index("await_ci")
    assert "wait_for_hosts" not in _names(calls)
    assert "tick" not in _names(calls)


def test_a_red_master_ci_never_renders(land_run):
    rc, _, _, calls, _ = land_run([], _hosts_only(await_ci=[(1, "red")]))
    assert rc == 1
    assert "host_targets" not in _names(calls)


# -- the routes through deploy_phase -------------------------------------------------------


def test_a_hosts_only_landing_deploys_nothing_and_waits_for_the_host(land_run):
    rc, out, _, calls, _ = land_run([], _hosts_only(host_targets=[PI]))
    assert (
        rc == 0
        and f"VERDICT: settled (PR #999, {MERGE_SHA}, tags: , hosts: daniel-pi)" in out
    )
    assert "deploy" not in _names(calls)
    # The tick was awaited at step 4, because the PR has no tag.
    assert _names(calls).index("tick") < _names(calls).index("wait_for_hosts")
    [wait] = [c for c in calls if c[0] == "wait_for_hosts"]
    assert wait[1][:2] == (["daniel-pi"], MERGE_SHA)


def test_a_plane_with_host_targets_still_needs_a_hand_and_names_the_host(land_run):
    fakes = _hosts_only(host_targets=[PI], plane="`ansible-playbook x.yml`")
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 1 and "VERDICT: needs-manual-apply" in out
    assert "reaches no service tag, but is not done" in out
    assert "the package of daniel-pi; this landing did not wait" in out
    assert "wait_for_hosts" not in _names(calls)


def test_a_host_wait_that_defers_ends_the_landing_deferred(land_run):
    fakes = _hosts_only(
        host_targets=[PI], host_wait_outcome=("deferred", 75, "PR #999 — not armed")
    )
    rc, out, _, _, _ = land_run([], fakes)
    assert rc == 75 and "VERDICT: deferred (PR #999 — not armed)" in out


def test_a_tag_and_a_host_deploy_then_wait(land_run):
    fakes = Fakes(
        gh_views=_files("ansible/roles/k8s/sonarr/defaults/main.yml", PI_PACKAGE),
        host_targets=[PI],
    )
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 0
    assert _names(calls).index("deploy") < _names(calls).index("wait_for_hosts")
    assert _names(calls).index("wait_for_hosts") < _names(calls).index("gate")
    assert "services deployed" not in out


# -- the step list -------------------------------------------------------------------------


def test_the_host_wait_sits_between_the_deploy_phase_and_the_verdict():
    from deploy_tools.land_lib import pipeline

    labels = [label for label, _ in pipeline._STEPS]
    assert (
        labels.index("waiting for hosts to apply")
        == labels.index("deploying what the tick deferred") + 1
    )
    assert labels[-1] == "health verdict"


# -- the verdict wording -------------------------------------------------------------------


def _deployed(landing, tags, targets, **fakes):
    ln, _ = landing(Fakes(**fakes))
    ln.merge_sha, ln.resolved_tags, ln.host_targets = MERGE_SHA, tags, targets
    return ln


@pytest.mark.parametrize(
    ("tags", "targets", "wording"),
    [
        (["sonarr"], [], "services deployed"),
        ([], [PI], "hosts applied"),
        (["sonarr"], [PI], "services deployed and hosts applied"),
    ],
)
def test_the_verdict_names_what_the_landing_applied(landing, tags, targets, wording):
    ln = _deployed(landing, tags, targets)
    assert health_verdict.applied_wording(ln) == wording


def test_a_hosts_only_landing_never_reads_services_deployed(landing, capsys):
    """The tick applied a setup role of its own, and the wording still names the hosts."""
    ln = _deployed(landing, [], [PI], state={"behind_since": "1"})
    ln.classification = Classification(self_applied=True)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    out = capsys.readouterr().out + exc.value.detail
    assert "hosts applied" in out
    assert "services deployed" not in out


def test_the_no_tag_outcome_names_each_host_it_did_not_wait_on(landing, capsys):
    ln = _deployed(landing, [], [PI, HostTarget("daniel-box")])
    ln.classification = Classification(plane="`x`")
    with pytest.raises(Outcome):
        deploy.no_tag_outcome(ln)
    out = capsys.readouterr().out
    assert "daniel-pi" in out and "daniel-box" in out
