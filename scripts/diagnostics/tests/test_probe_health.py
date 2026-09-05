"""`probe.py health <svc>`: the Docker verdict, the rollout verdict, and the role rollup.

It exits 0 only when the workload is fully rolled out AND nothing restarted in the last 180s.
Both halves matter: readiness flips a Deployment to Available before a bad liveness probe starts
killing it, so a rollout check alone reports green on a crashlooping pod. An unreadable restart
time counts as recent, so the gate fails closed.

The other two thirds of this gate have their own modules: `test_probe_health_resolver.py` for
the deploy-tag-to-workload resolution and the pod selector, `test_probe_health_cronjobs.py` for
the CronJob path. Shared fakes are in `_probe_health_fixtures.py`.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health.py
"""

from _probe_health_fixtures import NOW, pods

from diagnostics.probe_lib import health, health_docker, health_rollout
from diagnostics.probe_lib import health_kubectl


def _inspect(state, restarts=0):
    return [{"State": state, "RestartCount": restarts}]


def test_inspect_argv():
    assert health_docker.inspect_argv("jellyfin") == ["docker", "inspect", "jellyfin"]


def test_health_running_and_healthy_exits_zero():
    data = _inspect(
        {
            "Status": "running",
            "Health": {
                "Status": "healthy",
                "FailingStreak": 0,
                "Log": [{"Output": "ok\n"}],
            },
        }
    )
    text, code = health_docker.format_health(data, "jellyfin")
    assert code == 0
    assert "healthy" in text and "running" in text


def test_health_unhealthy_exits_one_and_shows_streak_and_last_log():
    data = _inspect(
        {
            "Status": "running",
            "Health": {
                "Status": "unhealthy",
                "FailingStreak": 3,
                "Log": [{"Output": "connection refused\n"}],
            },
        }
    )
    text, code = health_docker.format_health(data, "qbittorrent")
    assert code == 1
    assert "unhealthy" in text and "3" in text and "connection refused" in text


def test_health_no_healthcheck_running_exits_zero():
    text, code = health_docker.format_health(_inspect({"Status": "running"}), "valheim")
    assert code == 0
    assert "no healthcheck" in text


def test_health_exited_exits_one():
    text, code = health_docker.format_health(_inspect({"Status": "exited"}), "valheim")
    assert code == 1
    assert "exited" in text


def test_health_absent_and_undeclared_is_clean():
    """An undeclared name is a block tag or a typo — the one absence the notifier may skip."""
    text, code = health_docker.format_health([], "nope", declared=False)
    assert code == 1
    assert "not a declared service on any host" in text


def test_health_absent_but_declared_is_flagged():
    """The reject half.

    A service daniel-pi's inventory declares, with no container on the host, is a deploy that did
    not create it — and until 2026-09-01 it shared the undeclared case's "not found (not created"
    message, which the notifier skipped.
    """
    text, code = health_docker.format_health([], "wg-easy", declared=True)
    assert code == 1
    assert "MISSING" in text
    assert "not a declared service on any host" not in text


#
# daniel-pi's inventory is the population an absent container is measured against — the Pi is
# the only Docker host left. `declared_on_pi` takes the inventory path as an argument so this
# reads the fail-closed branch by passing one, rather than by patching a module global.


def test_declared_on_pi_reads_the_pis_containers_list():
    assert health_docker.declared_on_pi("wg-easy") is True
    assert health_docker.declared_on_pi("definitely-not-a-service") is False


def test_declared_on_pi_fails_closed_on_an_unreadable_inventory(tmp_path):
    """Fail closed: an unreadable inventory must not turn a missing container into a skip."""
    assert health_docker.declared_on_pi("wg-easy", tmp_path / "gone.yml") is True


#
# `health` ran `docker inspect` unconditionally until 2026-08-16 and had been dead on both
# cluster nodes since the 2026-08-14 Docker retirement — neither has the binary, so it raised
# FileNotFoundError. Every case below is a way the k8s replacement could report healthy when it
# is not, which is the only direction that matters for a post-deploy gate.


def _deploy(generation=1, observed=1, replicas=1, updated=1, ready=1, available=1):
    return {
        "metadata": {"generation": generation},
        "spec": {"replicas": replicas},
        "status": {
            "observedGeneration": observed,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
        },
    }


def test_k8s_health_rolled_out_and_quiet_exits_zero():
    text, code = health_rollout.format_k8s_health(
        _deploy(), pods(("app", 0, None)), "freshrss", NOW
    )
    assert code == 0
    assert "1/1 ready" in text


def test_k8s_health_missing_deployment_exits_one():
    text, code = health_rollout.format_k8s_health(None, None, "nope", NOW)
    assert code == 1
    assert "no Deployment" in text


def test_k8s_health_stale_generation_exits_one():
    """The controller has not observed the spec change yet, so the OLD pod is what is ready."""
    text, code = health_rollout.format_k8s_health(
        _deploy(generation=5, observed=4), pods(("app", 0, None)), "freshrss", NOW
    )
    assert code == 1
    assert "not observed yet" in text


def test_k8s_health_incomplete_rollout_exits_one():
    text, code = health_rollout.format_k8s_health(
        _deploy(replicas=2, updated=1, ready=1, available=1),
        pods(("app", 0, None)),
        "freshrss",
        NOW,
    )
    assert code == 1
    assert "rollout incomplete" in text


def test_k8s_health_recent_restart_exits_one_despite_being_ready():
    """The kube-state-metrics failure of 2026-08-07: a recent restart exits 1 despite being Ready.

    A bad liveness probe passes READINESS, flips the Deployment to Available, and only then starts
    getting killed. Every readiness-derived field reads healthy while the pod crashloops.
    """
    just_now = "2026-08-16T11:59:30Z"
    text, code = health_rollout.format_k8s_health(
        _deploy(), pods(("app", 3, just_now)), "kube-state-metrics", NOW
    )
    assert code == 1
    assert "RECENT RESTART" in text and "30s ago" in text


def test_k8s_health_old_restart_does_not_fail():
    """A pod that restarted last week and has been up since is healthy — restartCount alone
    would fail it forever."""
    last_week = "2026-08-09T12:00:00Z"
    text, code = health_rollout.format_k8s_health(
        _deploy(), pods(("app", 3, last_week)), "freshrss", NOW
    )
    assert code == 0
    assert "restarts=3" in text


def test_k8s_health_unparseable_restart_timestamp_does_not_fail_open():
    """An unreadable finishedAt must count as RECENT, not as 'long ago'.

    Treating unknown as old is the one direction a gate must never fail. Reachable whenever
    kubectl's timestamp format shifts — fractional seconds, for instance, parse as None.
    """
    assert health_rollout.seconds_since("not-a-timestamp", NOW) is None
    assert health_rollout.seconds_since(None, NOW) is None

    text, code = health_rollout.format_k8s_health(
        _deploy(), pods(("app", 1, "2026-08-16T11:59:30.123456Z")), "freshrss", NOW
    )
    assert code == 1
    assert "unreadable time" in text


def test_k8s_health_restart_with_no_laststate_fails_closed():
    """restartCount > 0 with no terminated state is still an unexplained restart."""
    text, code = health_rollout.format_k8s_health(
        _deploy(), pods(("app", 2, None)), "freshrss", NOW
    )
    assert code == 1
    assert "unreadable time" in text


def test_k8s_health_checks_every_container_in_the_pod():
    """A sidecar crashlooping while the main container is fine still fails the gate."""
    just_now = "2026-08-16T11:59:00Z"
    text, code = health_rollout.format_k8s_health(
        _deploy(), pods(("app", 0, None), ("sidecar", 9, just_now)), "n8n", NOW
    )
    assert code == 1
    assert "sidecar" in text


def _daemonset(generation=1, observed=1, desired=2, updated=2, ready=2, available=2):
    return {
        "kind": "DaemonSet",
        "metadata": {"generation": generation},
        "status": {
            "observedGeneration": observed,
            "desiredNumberScheduled": desired,
            "updatedNumberScheduled": updated,
            "numberReady": ready,
            "numberAvailable": available,
        },
    }


def test_k8s_health_reads_a_daemonset():
    """Six workloads here are DaemonSets — alloy, node-exporter, the crowdsec node agent.

    They carry the same four numbers under different status field names.
    """
    text, code = health_rollout.format_k8s_health(
        _daemonset(), pods(("app", 0, None)), "alloy", NOW
    )
    assert code == 0
    assert "2/2 ready" in text


def test_k8s_health_daemonset_missing_a_node_exits_one():
    """Scheduled on 2 nodes, ready on 1 — a Deployment's readyReplicas would read 0 here, so
    the field mapping has to be per-kind rather than a shared default."""
    text, code = health_rollout.format_k8s_health(
        _daemonset(ready=1, available=1), pods(("app", 0, None)), "alloy", NOW
    )
    assert code == 1
    assert "rollout incomplete" in text


def test_k8s_health_argv_can_ask_for_a_daemonset():
    assert "daemonset" in health_kubectl.k8s_deploy_argv(
        "alloy", "homelab", kind="daemonset"
    )


def test_k8s_health_argv_targets_the_named_namespace():
    assert health_kubectl.k8s_deploy_argv("freshrss", "homelab")[:4] == [
        "k3s",
        "kubectl",
        "-n",
        "homelab",
    ]
    assert "app=freshrss" in health_kubectl.k8s_pods_argv("freshrss", "homelab")


def _module_imports(source):
    """Every module a Python source imports, whatever the import form."""
    import ast

    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_health_kubectl_imports_nothing_so_its_argv_shapes_need_no_cluster():
    """health_kubectl.py's docstring promises it imports no sibling and runs no command.

    That promise is why the ClusterIP pair stays in health_docker.py — the `# DECIDED:` marker
    above `k8s_service_ip_argv` cites this test. A docstring nobody enforces rots into a comment
    the first time someone moves a subprocess call in here, so assert it.
    """
    from pathlib import Path

    assert _module_imports(Path(health_kubectl.__file__).read_text()) == set()
    # Non-vacuity: an empty or renamed module would also import nothing.
    assert {
        "WORKLOAD_KINDS",
        "k8s_deploy_argv",
        "k8s_pods_argv",
        "pod_selector",
    } <= set(vars(health_kubectl))


def test_the_import_census_sees_a_module_that_reaches_for_a_sibling():
    """The rejecting half: the guard above is only meaningful if it can go red."""
    assert _module_imports("from diagnostics.probe_lib import core\n") == {
        "diagnostics.probe_lib"
    }
    assert _module_imports("import subprocess\n") == {"subprocess"}


#
# A deploy tag names a ROLE, not a workload. Everything below covers the resolution step added
# 2026-09-01: for eleven roles the tag is not the name of the thing to health-check, and for
# four of them it names no workload at all — so `probe.py health <tag>` reported "no Deployment
# or DaemonSet" and `deploy_detach_notify.py` skipped it. PR #685 landed VERDICT: settled with
# claude-otel's gate never having run.
#


def _target(namespace, kind, name, workload, pods_doc=None):
    """One entry of `format_role_health`'s `checked` list.

    `pods_doc`, not `pods`: the parameter would otherwise shadow the `pods()` fixture this
    module imports, and every call site passes `pods()` for it.
    """
    return (namespace, kind, name, workload, pods_doc)


def test_role_health_all_present_is_clean():
    text, code = health.format_role_health(
        "claude-otel",
        [
            _target("observability", "Deployment", "grafana", _deploy(), pods()),
            _target("observability", "Deployment", "loki", _deploy(), pods()),
        ],
        NOW,
    )
    assert code == 0
    assert "all 2 workloads healthy" in text
    assert "observability/grafana" in text


def test_role_health_absent_workload_is_flagged():
    """The safety-critical half.

    The role's manifests declare grafana; the cluster does not have it. That is a failed deploy, and
    it must NOT read as a skip.
    """
    text, code = health.format_role_health(
        "claude-otel",
        [
            _target("observability", "Deployment", "grafana", None),
            _target("observability", "Deployment", "loki", _deploy(), pods()),
        ],
        NOW,
    )
    assert code == 1
    assert "MISSING" in text
    assert text.splitlines()[0].startswith(
        "claude-otel: 1 of 2 workloads FAILED the gate"
    )


def test_role_health_unhealthy_sibling_is_flagged():
    """A workload the tag is NOT named after still fails the role.

    karakeep-time-tagger crashlooping behind a healthy `karakeep` was invisible before the
    resolution step.
    """
    text, code = health.format_role_health(
        "karakeep",
        [
            _target("homelab", "Deployment", "karakeep", _deploy(), pods()),
            _target(
                "homelab",
                "Deployment",
                "karakeep-time-tagger",
                _deploy(ready=0, available=0),
                pods(),
            ),
        ],
        NOW,
    )
    assert code == 1
    assert "karakeep-time-tagger" in text.splitlines()[0]


def test_role_health_verdict_rides_the_first_line():
    """deploy_detach_notify reads `splitlines()[0]`, so a failure buried in the per-workload
    detail would be reported as the summary line's verdict instead."""
    text, _ = health.format_role_health(
        "scrutiny",
        [
            _target("homelab", "Deployment", "scrutiny-web", None),
            _target("homelab", "Deployment", "scrutiny-influxdb", _deploy(), pods()),
        ],
        NOW,
    )
    assert "FAILED" in text.splitlines()[0]
