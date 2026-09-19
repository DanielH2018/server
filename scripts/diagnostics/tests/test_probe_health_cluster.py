"""`probe.py health --cluster`: the gate must be about the cluster it says it is.

Run after a `-e target=daniel-stage` deploy on daniel-box, the gate used to read whatever the
local kubectl served — PRODUCTION — and exited 0: a healthy verdict about a cluster the deploy
never touched (#1663). That is worse than no gate: an inert check reports nothing, this one
reported a pass about the wrong subject.

The identity check itself lives in `lib.kubectl` since #2062 and is tested there
(`scripts/lib/tests/test_kubectl.py`). What stays here is the gate's own use of it: the
refusal comes first and is one line, and it is a failure rather than a skip when
`deploy_detach_notify.py` reads it.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_cluster.py
"""

from lib.kubectl import cluster_refusal

from diagnostics.probe_lib import health


def test_run_health_refuses_before_it_gates_anything(capsys):
    """The refusal comes first, so no kubectl runs and no workload is read.

    `served=` hands the gate an already-known cluster identity, which is what lets this assert
    the ordering without a cluster and without patching: a run that reached the gate would
    have to talk to kubectl, and the test host has none.
    """
    assert health.run_health("authelia", cluster="stage", served="prod") == 1
    assert "not stage" in capsys.readouterr().out


def test_run_health_still_gates_when_the_cluster_matches(capsys):
    """The accepting half. media-volume declares no rollout-checkable workload, so the gate
    reaches its own verdict by rendering manifests rather than by asking a cluster.

    That dependency is real: if a Deployment ever lands in the media-volume role, this test
    starts reaching for a cluster CI does not have. Pick another workload-free role then.
    """
    assert health.run_health("media-volume", cluster="prod", served="prod") == 1
    assert "no rollout-checkable workload" in capsys.readouterr().out


def test_a_refusal_is_routed_as_a_failure_not_a_skip():
    """The refusal crosses a subprocess boundary and is classified by substring.

    `deploy_detach_notify.py` reads the gate's first stdout line and turns a
    NOT_APPLICABLE_MARKERS match into a `skipped` verdict. A refusal means the gate did not
    run, which must fail the verdict rather than skip it — the same rule the absent-workload
    messages follow, and the only thing connecting these two modules is this assertion.
    """
    from deploy_tools import deploy_detach_notify as notify_mod

    for requested, served in (("stage", "prod"), ("prod", None)):
        message = cluster_refusal(requested, served)
        assert not [
            marker for marker in notify_mod.NOT_APPLICABLE_MARKERS if marker in message
        ], f"a refusal would be reported as `skipped`: {message}"
