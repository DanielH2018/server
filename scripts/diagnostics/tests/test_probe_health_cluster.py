"""`probe.py health --cluster`: the gate must be about the cluster it says it is.

Run after a `-e target=daniel-stage` deploy on daniel-box, a gate that read whatever the local
kubectl served would read PRODUCTION and exit 0: a healthy verdict about a cluster the deploy
never touched. That is worse than no gate: an inert check reports nothing, this one reported a
pass about the wrong subject.

The identity check itself lives in `lib.kubectl` and is tested there
(`scripts/lib/tests/test_kubectl.py`). What stays here is the gate's own use of it: the
refusal comes first and is one line, and it is a failure rather than a skip when
`deploy_lib/detach_notify.py` reads it.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_cluster.py
"""

import json
import subprocess
from pathlib import Path

from lib import kubectl
from lib.exit_codes import PROBE_NOT_APPLICABLE, PROBE_UNREADABLE

from diagnostics.probe_lib import health, health_alerts
from _probe_health_fixtures import current_checkout

# A Prometheus with no series at all, so these cases read the rollout verdict alone and
# never reach the network. The alert plane's own cases are in test_probe_health_alerts.py.
_QUIET = health_alerts.PromTools(query=lambda promql: [], sleep=lambda s: None)


def test_run_health_refuses_before_it_gates_anything(capsys):
    """The refusal comes first, so no kubectl runs and no workload is read.

    `served=` hands the gate an already-known cluster identity, which is what lets this assert
    the ordering without a cluster and without patching: a run that reached the gate would
    have to talk to kubectl, and the test host has none.
    """
    assert (
        health.run_health("authelia", cluster="stage", served="prod")
        == PROBE_UNREADABLE
    )
    assert "not stage" in capsys.readouterr().err


def test_run_health_still_gates_when_the_cluster_matches(capsys):
    """The accepting half. media-volume declares no rollout-checkable workload, so the gate
    reaches its own verdict by rendering manifests rather than by asking a cluster.

    That dependency is real: if a Deployment ever lands in the media-volume role, this test
    starts reaching for a cluster CI does not have. Pick another workload-free role then.
    """
    assert (
        health.run_health(
            "media-volume",
            cluster="prod",
            served="prod",
            prom=_QUIET,
            stale_reason=current_checkout,
        )
        == PROBE_NOT_APPLICABLE
    )
    assert "no rollout-checkable workload" in capsys.readouterr().out


def test_a_role_with_no_workload_exits_the_skip_code(capsys):
    """netpol-baseline renders NetworkPolicies and probe Jobs, nothing with a rollout (#3596).

    Exit 1 made `probe.py health netpol-baseline` unpassable on a healthy deploy, and exit 0
    hid the skip from the deploy notifier, which grades on the code (#7895).
    """
    assert (
        health.run_health(
            "netpol-baseline",
            cluster="prod",
            served="prod",
            prom=_QUIET,
            stale_reason=current_checkout,
        )
        == PROBE_NOT_APPLICABLE
    )
    assert "no rollout-checkable workload" in capsys.readouterr().out


def _kubectl(deployment):
    """kubectl that serves `deployment` for any Deployment read and NotFound for the rest."""

    nodes = {
        "items": [{"metadata": {"name": n}} for n in ("daniel-box", "daniel-server")]
    }

    def run(argv, timeout):
        if argv[-4:] == ["get", "nodes", "-o", "json"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(nodes), "")
        if deployment is not None and ("deploy" in argv or "deployment" in argv):
            return subprocess.CompletedProcess(argv, 0, json.dumps(deployment), "")
        if deployment is not None and "pods" in argv:
            return subprocess.CompletedProcess(argv, 0, '{"items": []}', "")
        return subprocess.CompletedProcess(argv, 1, "", "Error (NotFound): x")

    return kubectl.Tools(
        run=run,
        find_tool=lambda name: "/usr/local/bin/kubectl",
        find_kubeconfig=lambda: Path("/tmp/kubeconfig"),
    )


def _guessed(tmp_path, deployment):
    """run_health on a tag no k8s role declares, so the tag itself is the guessed name."""
    return health.run_health(
        "no-such-role",
        cluster="prod",
        served="prod",
        tools=_kubectl(deployment),
        roles_dir=tmp_path,
        prom=_QUIET,
        stale_reason=current_checkout,
    )


def test_a_guessed_name_with_no_workload_exits_the_skip_code(tmp_path, capsys):
    """A tag that names no cluster workload is skipped, which lets `--docker` take over."""
    assert _guessed(tmp_path, None) == PROBE_NOT_APPLICABLE
    assert "no Deployment or DaemonSet" in capsys.readouterr().out


def test_a_guessed_name_whose_workload_is_unhealthy_fails(tmp_path, capsys):
    """The reject half: a guessed workload that exists is graded, never skipped."""
    stuck = {
        "metadata": {"generation": 2},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "x"}}},
        "status": {"observedGeneration": 1, "readyReplicas": 0},
    }
    assert _guessed(tmp_path, stuck) == 1
