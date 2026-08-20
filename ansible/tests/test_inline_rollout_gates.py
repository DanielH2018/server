"""Guards the two roles that must wait for their own rollout inline.

`k8s/manifests` does not wait. It queues the rollout for `k8s/rollout-drain`, which drains the
whole batch at the end — the change that turned 1386s of serial waiting into one max(). Almost
every role tolerates that, because nothing in them touches the workload they just rolled.

Two do. `crowdsec` execs into `deploy/crowdsec` to register agents on the LAPI; `registry`
dials `deploy/registry` over its Service from three self-test Jobs. For those, the queued
rollout lands after the tasks that depend on it, so each needs its own `rollout status` first.

Both were found the same way — by a deploy failing against the pod it had just restarted:

  * crowdsec, 2026-08-16: 2 of 4 registrations landed, then the pod went down mid-loop, and
    `no_log` censored the body into a bare "non-zero return code".
  * registry, 2026-08-20 13:49: the push self-test got `connection refused` on its first pod
    and passed on its retry, spending the whole of `backoffLimit: 1` to stay green.

That second one is why this file exists rather than a third comment. The registry case never
failed a deploy, so nothing would have caught it drifting back.

`rollout status`, never `wait --for=condition=Available` — every Deployment here is
single-replica, so the OLD pod satisfies Available and the wait returns instantly against a
rollout that has not started.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_K8S_ROLES = _REPO / "ansible/roles/k8s"

# role -> the Deployment its own tasks talk to after the manifests include.
_MUST_GATE = {
    "crowdsec": "deploy/crowdsec",
    "registry": "deploy/registry",
}


def _tasks(role: str) -> list[dict]:
    return yaml.safe_load((_K8S_ROLES / role / "tasks" / "main.yml").read_text()) or []


def _cmd(task: dict) -> str:
    for key in ("ansible.builtin.command", "ansible.builtin.shell"):
        module = task.get(key)
        if isinstance(module, dict):
            return str(module.get("cmd", ""))
        if isinstance(module, str):
            return module
    return ""


def _gate_index(tasks: list[dict], target: str) -> int:
    for index, task in enumerate(tasks):
        cmd = _cmd(task)
        if "rollout status" in cmd and target in cmd:
            return index
    return -1


def test_both_roles_gate_on_their_own_rollout() -> None:
    for role, target in _MUST_GATE.items():
        tasks = _tasks(role)
        assert _gate_index(tasks, target) >= 0, (
            f"{role} no longer waits for {target} before using it. k8s/manifests queues the "
            "rollout for the end of the batch, so without this the role runs against the pod "
            "it just restarted."
        )


def test_the_gate_precedes_everything_that_uses_the_workload() -> None:
    for role, target in _MUST_GATE.items():
        tasks = _tasks(role)
        gate = _gate_index(tasks, target)
        name = target.split("/", 1)[1]
        users = [
            index
            for index, task in enumerate(tasks)
            if index != gate
            and name in _cmd(task)
            and "rollout status" not in _cmd(task)
        ]
        assert users, (
            f"{role}: no task uses {name} any more; re-check whether the gate is needed"
        )
        assert gate < min(users), (
            f"{role}: the rollout gate runs at task {gate}, after task {min(users)} which "
            f"already uses {name}. The gate has to come first to be a gate."
        )


def test_the_gate_uses_rollout_status_not_a_readiness_wait() -> None:
    for role, target in _MUST_GATE.items():
        cmd = _cmd(_tasks(role)[_gate_index(_tasks(role), target)])
        assert "condition=Available" not in cmd, (
            f"{role}: every Deployment here is single-replica, so the OLD pod satisfies "
            "Available and this returns instantly against a rollout that has not begun."
        )
        assert "--timeout=" in cmd, f"{role}: an ungated wait hangs the deploy: {cmd}"


def test_the_gate_is_tagged_with_what_it_protects() -> None:
    # `[deploy]` alone, not `[config, deploy]`: tags union for SELECTION but exclusion matches
    # ANY tag, so a dual-tagged gate vanishes under the documented `--skip-tags deploy` and the
    # config-only run silently loses it.
    for role, target in _MUST_GATE.items():
        tasks = _tasks(role)
        gate = tasks[_gate_index(tasks, target)]
        assert gate.get("tags") == ["deploy"], (
            f"{role}: the gate is tagged {gate.get('tags')!r}; it protects deploy-tagged tasks "
            "and must carry exactly that tag."
        )
