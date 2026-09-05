#!/usr/bin/env python3
"""longhorn-manager and the csi-* sidecars must carry a memory REQUEST and no LIMIT (#1243).

Upstream's deploy/longhorn.yaml ships every one of these at `resources: {}` — BestEffort QoS,
oom_score_adj 1000 — so the kernel eats the storage control plane first under any memory
pressure. The role's own `k3s kubectl apply -f https://.../longhorn.yaml` re-pulls that empty
block on every run (there's no local copy of the upstream manifest to edit), so the fix has to
be a patch task the role runs AFTER the apply, not a template change.

Only a request, never a limit: a limit set too low turns "killed by OOM badness" into "killed
by cgroup cap" — a new failure mode the diagnosis rejected outright. `test_a_patch_carrying_a_
limits_key_is_rejected` is the pair proving this guard can actually go red rather than
passing on any resources block it is handed.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_control_plane_resources.py
"""

import json

import pytest
from lib import yaml_fast

from _helpers import SETUP_ROLES

K3S = SETUP_ROLES / "k3s"
LONGHORN_TASKS = K3S / "tasks" / "longhorn.yml"
K3S_DEFAULTS = yaml_fast.safe_load((K3S / "defaults" / "main.yml").read_text())

CSI_SIDECARS = ("csi-attacher", "csi-provisioner", "csi-resizer", "csi-snapshotter")


def _tasks():
    return yaml_fast.safe_load(LONGHORN_TASKS.read_text())


def _task_named(name):
    for task in _tasks():
        if task.get("name") == name:
            return task
    raise AssertionError("no task named %r in %s" % (name, LONGHORN_TASKS))


def _index_of(name):
    names = [t.get("name") for t in _tasks()]
    assert name in names, "no task named %r in %s" % (name, LONGHORN_TASKS)
    return names.index(name)


def test_csi_sidecar_patch_runs_after_the_driver_deployer_wait():
    """THE ORDERING BUG THIS PINS: none of the four csi-* Deployments is in longhorn.yaml.

    grep of the pinned upstream manifest (v1.12.1) confirms it: `csi-attacher` etc. appear only
    as image-tag env vars on longhorn-driver-deployer, which creates the real Deployments at
    runtime once it starts — the same asynchronous-appearance race
    `k3s_join_longhorn_sched`/`until: rc == 0` rides out in tasks/agent_verify.yml. Patching
    them any earlier than the driver-deployer wait 404s on a fresh bring-up.
    """
    assert _index_of("Wait for the Longhorn driver deployer") < _index_of(
        "Give the Longhorn CSI sidecars a memory request so they leave BestEffort"
    )


def test_csi_sidecar_patch_tolerates_the_deployments_not_existing_yet():
    task = _task_named(
        "Give the Longhorn CSI sidecars a memory request so they leave BestEffort"
    )
    assert task.get("until"), (
        "no until: — a patch issued before the driver-deployer finishes creating the "
        "Deployment fails outright instead of retrying"
    )
    assert task.get("retries", 0) > 1


def test_manager_patch_runs_after_longhorn_is_installed():
    # longhorn-manager IS in the static manifest (a DaemonSet), so this one only has to follow
    # the apply — no driver-deployer race to ride out.
    assert _index_of("Install Longhorn") < _index_of(
        "Give longhorn-manager a memory request so it leaves BestEffort"
    )


def _render_patch(template, **subs):
    """The `vars.*_patch` Jinja string with its variable(s) substituted, then JSON-parsed.

    A plain string substitution, not a Jinja render: the two templates here interpolate a
    single scalar each ({{ k3s_longhorn_manager_memory_request }} or {{ item }}), so a real
    Jinja environment would be more machinery than the substitution it is checking.
    """
    rendered = template
    for name, value in subs.items():
        rendered = rendered.replace("{{ %s }}" % name, value)
    return json.loads(rendered)


def _container_resources(patch_doc):
    containers = patch_doc["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, "patch must touch exactly one named container"
    return containers[0]["resources"]


def _assert_request_only(resources):
    """The shared assertion: a memory request, and nothing under `limits`."""
    assert "memory" in resources.get("requests", {}), (
        "resources block carries no memory request: %r" % resources
    )
    assert "limits" not in resources, (
        "resources block carries a limit (%r) — a limit set too low turns an OOM-by-badness "
        "kill into an OOM-by-cgroup-cap kill, which #1243's diagnosis explicitly rejected"
        % resources
    )


def test_manager_patch_sets_a_request_and_no_limit():
    task = _task_named("Give longhorn-manager a memory request so it leaves BestEffort")
    patch = _render_patch(
        task["vars"]["manager_patch"],
        k3s_longhorn_manager_memory_request=str(
            K3S_DEFAULTS["k3s_longhorn_manager_memory_request"]
        ),
    )
    resources = _container_resources(patch)
    _assert_request_only(resources)
    assert resources["requests"]["memory"] == str(
        K3S_DEFAULTS["k3s_longhorn_manager_memory_request"]
    )


def test_manager_patch_names_the_longhorn_manager_container():
    task = _task_named("Give longhorn-manager a memory request so it leaves BestEffort")
    patch = _render_patch(
        task["vars"]["manager_patch"],
        k3s_longhorn_manager_memory_request="256Mi",
    )
    containers = patch["spec"]["template"]["spec"]["containers"]
    assert containers[0]["name"] == "longhorn-manager"


@pytest.mark.parametrize("sidecar", CSI_SIDECARS)
def test_csi_sidecar_patch_sets_a_request_and_no_limit(sidecar):
    task = _task_named(
        "Give the Longhorn CSI sidecars a memory request so they leave BestEffort"
    )
    patch = _render_patch(
        task["vars"]["sidecar_patch"],
        item=sidecar,
        k3s_longhorn_csi_sidecar_memory_request=str(
            K3S_DEFAULTS["k3s_longhorn_csi_sidecar_memory_request"]
        ),
    )
    containers = patch["spec"]["template"]["spec"]["containers"]
    assert containers[0]["name"] == sidecar
    _assert_request_only(containers[0]["resources"])


def test_csi_sidecar_task_loops_over_all_four_standard_sidecars():
    # Non-vacuity: a guard keyed to CSI_SIDECARS above would pass trivially if the task's own
    # loop silently dropped one — assert the loop itself, not just what each element renders to.
    task = _task_named(
        "Give the Longhorn CSI sidecars a memory request so they leave BestEffort"
    )
    assert set(task["loop"]) == set(CSI_SIDECARS)


def test_a_patch_carrying_a_limits_key_is_rejected():
    """THE RED PROOF: _assert_request_only must fail closed on a resources block with a limit.

    Without this, the two accept-side tests above could pass by construction — the shared
    assertion could be checking nothing at all and every real patch would still read green.
    """
    with pytest.raises(AssertionError, match="limit"):
        _assert_request_only(
            {"requests": {"memory": "256Mi"}, "limits": {"memory": "256Mi"}}
        )


def test_a_patch_missing_a_request_is_rejected():
    with pytest.raises(AssertionError, match="memory request"):
        _assert_request_only({})
