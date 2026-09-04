"""Step 5: one deploy.sh per declaring host, the lock and stale retries, the deploy verdicts.

Run: uv run pytest scripts/deploy_tools/tests/test_land_deploy.py
"""

from __future__ import annotations

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome


def _ready(landing, fakes=None, **opts):
    ln, calls = landing(fakes, **opts)
    ln.merge_sha = MERGE_SHA
    ln.ledger.t_ci = 2.0
    ln.ledger.t_tick = 3.0
    return ln, calls


def test_each_tag_deploys_on_the_host_that_declares_it(landing, capsys):
    """Issue #929: `--tags alloy` on daniel-box matched no service; the Pi ran the old container."""
    ln, calls = _ready(landing, Fakes(hosts="daniel-box\tsonarr\ndaniel-pi\talloy\n"))
    ln.tags = "sonarr,alloy"
    assert deploy.deploy_by_host(ln) == 0
    assert [c[1] for c in calls if c[0] == "deploy"] == [
        (PRIMARY, "sonarr", None),
        (PRIMARY, "alloy", "daniel-pi"),
    ]
    assert "deploying there with -e target=daniel-pi" in capsys.readouterr().out


def test_tags_no_host_declares_fall_through_to_one_deploy(landing):
    ln, calls = _ready(landing, Fakes(hosts=""))
    ln.tags = "k8s-manifests"
    deploy.deploy_by_host(ln)
    assert [c[1] for c in calls if c[0] == "deploy"] == [
        (PRIMARY, "k8s-manifests", None)
    ]


def test_a_retry_resumes_at_the_host_that_failed(landing):
    ln, calls = _ready(
        landing,
        Fakes(hosts="daniel-box\tsonarr\ndaniel-pi\talloy\n", deploy=[0, 75, 0]),
    )
    ln.tags = "sonarr,alloy"
    assert deploy.deploy_with_lock_retry(ln) == 0
    assert [c[1][1:] for c in calls if c[0] == "deploy"] == [
        ("sonarr", None),
        ("alloy", "daniel-pi"),
        ("alloy", "daniel-pi"),
    ]
    assert ln.ledger.lock_waited > 0


def test_the_lock_holder_is_sampled_before_the_deploy_attempt(landing):
    """#1031, the deploy half of the tick's own guard: the sample precedes the attempt.

    The fake answers once and then goes empty, so a post-attempt read would book nothing --
    but the ORDER assertion is what actually discriminates, since a post-attempt read
    consumes the same first answer.
    """
    ln, calls = _ready(
        landing, Fakes(deploy=[75, 0], lock_holder=["42 flock deploy", ""])
    )
    ln.tags = "sonarr"
    deploy.deploy_with_lock_retry(ln)
    names = [c[0] for c in calls]
    assert names.index("lock_holder") < names.index("deploy")
    assert ln.ledger.lock_holder == "42 flock deploy"


def test_a_mapping_failure_dies_with_its_own_verdict(landing):
    """Closes #1016: this used to surface as a bare `deploy-failed (exit 1)`."""
    ln, calls = _ready(landing, Fakes(hosts_rc=1))
    ln.tags = "sonarr"
    with pytest.raises(Outcome) as exc:
        deploy.deploy_by_host(ln)
    assert exc.value.verdict == "deploy-failed"
    assert (
        "deploy_tags.py hosts failed before any deploy.sh ran; nothing was touched"
        in exc.value.error
    )
    assert ln.ledger.cause == "host-lookup"
    assert "deploy" not in [c[0] for c in calls]


@pytest.mark.parametrize(
    "rc, verdict, code, phrase, cause",
    [
        (
            2,
            "deploy-failed",
            1,
            "a derived tag matched no service, so nothing deployed",
            "tag-miss",
        ),
        (20, "deploy-failed", 1, "some changes are live", "playbook-failed"),
        (75, "lock-busy", 75, "lock stayed busy", ""),
        (9, "deploy-failed", 1, "exit 9", "deploy-exit-9"),
    ],
)
def test_deploy_outcomes(landing, rc, verdict, code, phrase, cause):
    """#1031: every deploy-failed names its cause, so Loki can tell the shapes apart."""
    ln, _ = _ready(landing)
    ln.tags = "sonarr"
    with pytest.raises(Outcome) as exc:
        deploy.deploy_outcome(ln, rc)
    assert (exc.value.rc, exc.value.verdict) == (code, verdict)
    assert phrase in (exc.value.detail + (exc.value.error or ""))
    assert ln.ledger.cause == cause


def test_a_clean_deploy_returns(landing):
    ln, _ = _ready(landing)
    deploy.deploy_outcome(ln, 0)


@pytest.mark.parametrize(
    "fakes, verdict, code",
    [
        (Fakes(plane="initial_setup.yml --tags k3s"), "needs-manual-apply", 1),
        (Fakes(), "nothing-to-deploy", 0),
        (Fakes(self_applied=True, state={"hold_sha": "abc"}), "deploy-failed", 1),
        (Fakes(self_applied=True, state={"behind_since": "x"}), "deferred", 75),
        (Fakes(self_applied=True), "settled", 0),
        (
            Fakes(self_applied=True, remaining_setup="daniel-server, daniel-pi"),
            "needs-manual-apply",
            1,
        ),
    ],
)
def test_no_tag_outcomes(landing, fakes, verdict, code):
    ln, _ = _ready(landing, fakes)
    ln.plane, ln.self_applied = fakes.plane, fakes.self_applied
    ln.remaining_setup = fakes.remaining_setup
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    assert (exc.value.rc, exc.value.verdict) == (code, verdict)
    if verdict == "needs-manual-apply" and fakes.remaining_setup:
        assert "other hosts still need it" in exc.value.detail
    assert ln.ledger.cause == ("tick-held" if fakes.state.get("hold_sha") else "")


def test_the_diff_fallback_derives_after_the_tick(landing):
    ln, calls = _ready(landing, Fakes(changed="sonarr"), since="abc")
    ln.needs_diff = True
    deploy.deploy_phase(ln)
    assert ln.tags == "sonarr"
    assert ("deploy_tags", ("changed", "abc"), {"cwd": PRIMARY}) in calls


def test_a_broad_diff_fallback_is_handed_to_a_hand(landing):
    ln, _ = _ready(landing, Fakes(changed_rc=3), since="abc")
    ln.needs_diff = True
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert exc.value.rc == 1 and "deploy it by hand" in exc.value.error


def test_a_stale_tree_waits_on_the_tip_before_reticking(landing):
    """Exit 4 is a resume point. Three landings on 2026-09-02 re-ticked immediately, deferred
    again and exited 4 again; the fix re-checks blockers, waits on the NEW tip, then ticks."""
    tip = "feedfacefeedfacefeedfacefeedfacefeedface"
    ln, calls = _ready(landing, Fakes(deploy=[4, 0], tip=tip))
    ln.tags = "sonarr"
    deploy.deploy_phase(ln)
    first = next(i for i, c in enumerate(calls) if c[0] == "deploy")
    tail = [c[1][0] if c[0] == "deploy_tags" else c[0] for c in calls[first + 1 :]]
    assert (
        tail.index("blockers")
        < tail.index("await_ci")
        < tail.index("tick")
        < tail.index("deploy")
    )
    assert next(c for c in calls[first:] if c[0] == "await_ci")[1][0] == tip


def test_the_tip_wait_is_booked_under_wait_ci_not_deploy(landing):
    ln, _ = _ready(landing, Fakes(deploy=[4, 0], tip="f" * 40))
    ln.tags = "sonarr"
    t_ci, t_tick = ln.ledger.t_ci, ln.ledger.t_tick
    deploy.deploy_phase(ln)
    assert ln.ledger.t_ci > t_ci and ln.ledger.t_tick - ln.ledger.t_ci == t_tick - t_ci


def test_a_stale_retry_is_bounded(landing):
    ln, calls = _ready(landing, Fakes(deploy=[4]))
    ln.tags = "sonarr"
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert exc.value.verdict == "deploy-failed"
    assert [c[0] for c in calls].count("deploy") == 4  # first attempt + 3 stale retries


def test_a_blocker_landing_during_the_wait_ends_the_retry_as_blocked(landing):
    ln, _ = _ready(landing, Fakes(deploy=[4], blockers=[3]))
    ln.tags = "sonarr"
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert exc.value.verdict == "blocked"


def test_a_contended_retick_inside_the_stale_retry_is_booked(landing):
    """The #1013 case: the retry's tick loses the lock and must retry and book it, not carry on."""
    ln, calls = _ready(landing, Fakes(deploy=[4, 0], tick=[3, 0]))
    ln.tags = "sonarr"
    deploy.deploy_phase(ln)
    assert [c[0] for c in calls].count("tick") == 2 and ln.ledger.lock_waited > 0


def test_deploy_phase_stamps_the_ledger(landing):
    ln, _ = _ready(landing)
    ln.tags = "sonarr"
    deploy.deploy_phase(ln)
    assert ln.ledger.tags_label == "sonarr" and ln.ledger.t_deploy is not None
