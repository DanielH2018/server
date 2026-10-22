"""The roles that declare `manifests_self_rollouts` follow the record and the apply.

No live role restarts its workloads through a private task since observability and pihole
flipped to Flux, where the renderer's restart annotation rolls them (D5.1e). These properties
are pinned here, over the harness in `_release_expectation.py`:

They declare those workloads to the record as `manifests_self_rollouts`, and the record
decides `restart` for them from the same facts (the red-proof pair: a changed render names
grafana `restart: true`, an unchanged one `false`).

The pod-template fingerprints cover every `manifests_self_rollouts` entry, and observability's
entries carry the namespace the fingerprints must read in -- the fingerprints default to
`k8s_namespace`, its workloads live in `observability`, and a read in the wrong namespace
fails on both sides and reads as "not rolled", which is the double roll.

Observability's entries all declare `restart_on: []`, so the record stamps `restart: false`
for each. Its ConfigMaps are hashed into their own pod templates, so the apply rolls those.
The entries carry no `rolled_by_role`, which would drop them from the fingerprints.

Run: uv run pytest ansible/tests/k8s/test_self_rollouts_follow_the_apply.py
"""

from _helpers import load_yaml, render_expr, task_named, walk_tasks
from lib.repo_paths import ALL_VARS
from _release_expectation import (
    LOOP_EXPR,
    MAIN,
    OBSERVABILITY_NAMESPACE,
    observability_self_rollouts,
    rollouts as _rollouts,
)


def _pairs(entries):
    return {(e["kind"], e["name"]) for e in entries}


_SELF_ROLLOUT_READERS_IN_MAIN = (
    "Check that every manifests_extra_rollouts",
    "List the workloads whose pod template is fingerprinted",
)


def test_a_self_rolling_role_records_only_what_it_declares():
    """The var reaches the record and the fingerprints and nothing else: the shared
    restart's loop and the drain queue read `manifests_extra_rollouts`, never
    `manifests_self_rollouts`. Those roles opted out of both, and a declaration that
    re-enrolled the role would restart every telemetry workload at once."""
    readers = set()
    for task in walk_tasks(MAIN):
        text = str(task)
        if "manifests_self_rollouts" in text:
            assert task["name"].startswith(_SELF_ROLLOUT_READERS_IN_MAIN), task["name"]
            readers.add(task["name"].split(" for ")[0])
    assert len(readers) == len(_SELF_ROLLOUT_READERS_IN_MAIN), readers
    assert "manifests_self_rollouts" in LOOP_EXPR
    targets = task_named(MAIN, "List the workloads whose pod template is fingerprinted")
    fingerprinted = render_expr(
        targets["ansible.builtin.set_fact"]["manifests_fingerprint_targets"],
        manifests_service="observability",
        manifests_rollout="",
        manifests_rollout_kind="deploy",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=observability_self_rollouts(),
    )
    declared = observability_self_rollouts()
    assert {e["name"] for e in fingerprinted} == {e["name"] for e in declared}, (
        fingerprinted
    )
    assert "manifests_self_rollouts" not in str(
        task_named(MAIN, "Roll the extra deployments after")
    )
    assert "manifests_self_rollouts" not in str(
        task_named(MAIN, "Queue the batch drain for the extra rollouts")
    )


def _observability_rollouts(**over):
    return _rollouts(
        manifests_service="observability",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=observability_self_rollouts(),
        **over,
    )


def test_observability_a_changed_render_expects_nothing_to_roll():
    """The red half. Every workload here declares `restart_on` without `config`, because
    each ConfigMap is hashed into its own pod template — the apply rolls the one that changed
    and `manifests_rolled_by_apply` reports it, so no restart TASK runs and the record must
    expect no `restartedAt`. Expecting one is not a quiet failure: `probe.py health` reads it
    as NOT ROLLED and fails the deploy."""
    rollouts = _observability_rollouts(
        manifests_apply={"stdout": "deployment.apps/grafana configured"},
    )
    assert rollouts["otel-collector"]["kind"] == "daemonset"
    assert not any(r["restart"] for r in rollouts.values()), rollouts


def test_observability_a_changed_secret_expects_only_its_secret_readers_to_roll():
    """The stamp leaves a Secret's restart to the role. `manifests_secret_render` changes for
    any one Secret file, so a stamp that decided from it would hold every reader to a roll
    that an Alertmanager config edit never made. Every entry is stamped `restart: false`, and
    the renderer's restart annotation rolls the readers of the changed Secret alone."""
    rollouts = _observability_rollouts(
        manifests_render={"changed": False},
        manifests_secret_render={"changed": True},
        manifests_apply={"stdout": "deployment.apps/grafana configured"},
    )
    assert set(rollouts) >= {"prometheus", "grafana", "alertmanager"}, rollouts
    assert not any(r["restart"] for r in rollouts.values()), rollouts
    assert not any("rolled_by_role" in r for r in rollouts.values()), rollouts


def test_observability_an_unchanged_render_expects_nothing_to_roll():
    """An idempotent re-run names every workload with `restart: false`."""
    rollouts = _observability_rollouts(manifests_render={"changed": False})
    assert rollouts["grafana"]["restart"] is False
    assert not any(r["restart"] for r in rollouts.values()), rollouts


def test_observability_declares_the_namespace_the_fingerprints_must_read():
    """Its workloads live outside k8s_namespace, which the fingerprints default to. A
    declaration without the namespace passes every other test here and double-rolls in the
    play: both reads fail, every entry reads "not rolled", the private restart fires."""
    declared = observability_self_rollouts()
    assert declared, "the declaration rendered empty"
    assert {e["namespace"] for e in declared} == {OBSERVABILITY_NAMESPACE}, declared
    assert OBSERVABILITY_NAMESPACE != load_yaml(ALL_VARS)["k8s_namespace"], (
        "the trap this test guards no longer exists"
    )
