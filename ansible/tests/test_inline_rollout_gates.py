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


def _load(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text()) or []


def _tasks(role: str) -> list[dict]:
    """main.yml with included and imported task files inlined, so ordering survives the split.

    THE ORIGINAL VERSION READ main.yml AND NOTHING ELSE, which is why the four roles found on
    2026-08-27 were missed: crowdsec and registry do their work in main.yml, where the guard
    could see it, while jellyfin, tdarr, janitorr and qbittorrent do theirs in verify.yml behind
    an include. The gap was in the guard, not only in the roles.

    BOTH `include_tasks` AND `import_tasks` are followed. Reading only the first missed janitorr,
    which imports — the same narrowing this guard had already fallen for once, in larger form.
    The two forms differ in WHEN Ansible resolves the file, not in whether those tasks run, so a
    guard reading for shape has to treat them alike.

    Only a literal filename is followed. A templated include is not resolvable here and is left
    as the opaque task it is, rather than guessed at.
    """
    tasks_dir = _K8S_ROLES / role / "tasks"
    flat: list[dict] = []
    for task in _load(tasks_dir / "main.yml"):
        include = next(
            (
                task[key]
                for key in (
                    "ansible.builtin.include_tasks",
                    "ansible.builtin.import_tasks",
                )
                if key in task
            ),
            None,
        )
        name = include.get("file") if isinstance(include, dict) else include
        target = (
            tasks_dir / name if isinstance(name, str) and "{{" not in name else None
        )
        if target is not None and target.is_file():
            flat.extend(_load(target))
        else:
            flat.append(task)
    return flat


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


# ── the derived half ────────────────────────────────────────────────────────────────────────
#
# _MUST_GATE above is a hand-maintained list, and a hand-maintained list is why four roles sat
# ungated until a deploy failed. The checks below derive the roles that need a gate FROM THEIR
# OWN SHAPE, so the next role written this way fails the suite instead of waiting for an
# incident.
#
# The shape is narrow on purpose: a role that looks up a pod by its OWN app label is about to
# assert something about that pod, and k8s/manifests has queued rather than run its rollout.
# Roles that read OTHER workloads' pods are not candidates — claude-otel samples restart counts
# across a configured list, netpol-baseline reads an exemption label across the namespace, and
# neither is asserting about a pod it just rolled.

_SELF_POD_LOOKUP = "get pod"

# Derived on 2026-08-27. Named here so a derivation that silently stops matching fails loudly
# rather than passing an empty set — the failure mode recorded in
# memory/a-derivation-can-narrow-the-list-it-replaces.md, where replacing a hardcoded list by
# shape dropped an entry while reading as a widening.
_KNOWN_SELF_POD_ROLES = {"janitorr", "jellyfin", "qbittorrent", "tdarr"}


def _looks_up_own_pod(role: str, task: dict) -> bool:
    cmd = _cmd(task)
    return _SELF_POD_LOOKUP in cmd and f"app={role}" in cmd


def _self_pod_roles() -> dict[str, list[dict]]:
    found = {}
    for role_dir in sorted(_K8S_ROLES.iterdir()):
        if not (role_dir / "tasks" / "main.yml").is_file():
            continue
        tasks = _tasks(role_dir.name)
        if any(_looks_up_own_pod(role_dir.name, task) for task in tasks):
            found[role_dir.name] = tasks
    return found


def test_the_derivation_still_matches_the_roles_it_was_written_for() -> None:
    derived = set(_self_pod_roles())
    missing = _KNOWN_SELF_POD_ROLES - derived
    assert not missing, (
        f"the self-pod-lookup derivation no longer matches {sorted(missing)}. Either those "
        "roles changed shape, or the matcher broke and is now guarding less than it claims — "
        "check which before editing this list."
    )


def test_every_role_that_inspects_its_own_pod_gates_on_its_rollout() -> None:
    for role, tasks in _self_pod_roles().items():
        gate = _gate_index(tasks, role)
        first_lookup = next(
            index for index, task in enumerate(tasks) if _looks_up_own_pod(role, task)
        )
        assert gate >= 0, (
            f"{role} looks up its own pod at task {first_lookup} without ever waiting for its "
            "rollout. k8s/manifests QUEUES the rollout for k8s/rollout-drain, so this reads the "
            "pod being replaced, and every assertion after it describes the outgoing pod."
        )
        assert gate < first_lookup, (
            f"{role}: the rollout gate is at task {gate}, after the pod lookup at "
            f"{first_lookup}. A gate that runs second is not a gate."
        )


def test_no_role_gates_with_a_readiness_wait_on_its_own_pods() -> None:
    # The three primitives that LOOK like gates and are all satisfied by the outgoing pod.
    # janitorr shipped the middle one and read as gated for as long as nobody checked which pod
    # its proofs were describing.
    for role, tasks in _self_pod_roles().items():
        for task in tasks:
            cmd = _cmd(task)
            if "wait --for=" not in cmd or f"app={role}" not in cmd:
                continue
            assert False, (
                f"{role}: `{cmd.strip()}` is satisfied by the OLD pod — Ready and Available are "
                "both true of it until it stops, and status.phase stays Running while it is "
                "Terminating. Use `rollout status` instead."
            )
