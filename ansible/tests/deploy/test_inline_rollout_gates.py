"""A role that talks to a workload it just rolled must wait for that rollout first.

`k8s/manifests` does not wait. It queues the rollout for `k8s/rollout-drain`, which drains the
whole batch at the end — the change that turned 1386s of serial waiting into one max(). Almost
every role tolerates that, because nothing in them touches the workload they just rolled. The
ones that do read the OUTGOING pod, and every assertion they make describes it.

Two were found by a deploy failing against the pod it had just restarted:

  * crowdsec, 2026-08-16: 2 of 4 registrations landed, then the pod went down mid-loop, and
    `no_log` censored the body into a bare "non-zero return code".
  * registry, 2026-08-20 13:49: the push self-test got `connection refused` on its first pod
    and passed on its retry, spending the whole of `backoffLimit: 1` to stay green.

`rollout status`, never `wait --for=condition=Available` — every Deployment here is
single-replica, so the OLD pod satisfies Available and the wait returns instantly against a
rollout that has not started.

WHAT THIS FILE PROMISES
-----------------------
Two halves. `_MUST_GATE` is a hand-written pair; the derived half below finds the rest from
their own shape, so the next role written this way fails the suite rather than waiting for an
incident.

The derived half recognises three shapes of "inspects a pod", all of them via `kubectl`:

  1. `get pod -l app=<workload>`     — jellyfin, tdarr, janitorr, qbittorrent
  2. `get pod -l <anylabel>=<value>` — pihole, which selects an instance by the pod-only
                                       `instance` label because both its Deployments share
                                       `app: pihole` in an immutable selector
  3. `exec`/`logs <target>`          — crowdsec (`exec deploy/crowdsec`) and claude-otel
                                       (`exec deploy/grafana`), plus every `exec {{ reg.stdout }}`
                                       whose register is traced back to the lookup that filled it

The register trace follows the bare `{{ reg.stdout }}` form only. A filter or an index on it
(`{{ reg.stdout | trim }}`) is unresolved, not resolved-to-nothing, so it fails until excused.

WORKLOAD IDENTITY IS NOT THE DIRECTORY NAME. The first version of this guard matched
`app={role}` and then looked for the role's directory name inside a `rollout status` command.
That is true of exactly the four roles it was written for. `claude-otel` renders the workload
called `grafana`; `pihole` renders two, `pihole` and `pihole-2`, from one template and picks
between them at runtime. So both the inspection and the gate resolve to a workload NAME taken
from the role's RENDERED manifests (`_k8s_render.rendered_docs()`), and a gate satisfies an
inspection only when the two name the same workload. `exec deploy/grafana` is answered by
`rollout status deploy/grafana` and by nothing else.

A multi-workload inspection needs ALL of its targets gated, not any of them: `get pod -l
app=pihole` names both instances, and a gate for one of them is not a gate for the other.

UNRESOLVED TARGETS FAIL. A command whose target is a Jinja expression this file cannot resolve
is not silently skipped — silence is the failure mode this whole class is about. It must appear
in `_UNRESOLVED_TARGETS` with the reason, and that set is checked for staleness both ways.

WHAT IT DOES NOT COVER, deliberately:

  * Roles that render no Deployment/DaemonSet/StatefulSet (volume-claim, netpol-baseline,
    rollout-drain, media-volume, ...). They have no rollout of their own to gate on, and every
    pod they touch belongs to someone else.
  * `logs job/<name>` and other Job targets. A Job has no rollout for `rollout status` to gate.
  * Reaching a workload over its Service instead of `kubectl` — registry dials `deploy/registry`
    from three self-test Jobs, which no command text here can see. That is why registry stays
    in the hand-written `_MUST_GATE` and cannot be derived.
  * `when:` conditions. A gate that is skipped at runtime still counts as a gate here. pihole's
    is conditional on the manifests having changed, which is also the only run that rolls it.

WHERE THE MACHINERY LIVES. `_inline_rollout_tasks.py` expands a role's tasks — includes
followed, blocks flattened, literal loops unrolled. `_inline_rollout_targets.py` resolves a
task's target and a gate's target to workload names off the rendered manifests. This file is
the contract above, the hand-written `_MUST_GATE`, the excused `_UNRESOLVED_TARGETS`, and the
assertions.
"""

from __future__ import annotations


from _inline_rollout_tasks import _K8S_ROLES, _Task, _tasks
from _inline_rollout_targets import (
    _UNRESOLVED,
    _exec_target,
    _gate_indexes,
    _inspections,
    _kind_slash_name,
    _owned,
    _selector_targets,
    _tokens,
)


# role -> the workload its own tasks talk to after the manifests include.
_MUST_GATE = {
    "crowdsec": "crowdsec",
    "registry": "registry",
}


# ── what each role renders ──────────────────────────────────────────────────────────────────


# ── the role's tasks, flattened and loop-expanded ───────────────────────────────────────────


# ── reading a command's workload ────────────────────────────────────────────────────────────


# ── the hand-written pair ───────────────────────────────────────────────────────────────────


def test_both_roles_gate_on_their_own_rollout() -> None:
    for role, workload in _MUST_GATE.items():
        assert _gate_indexes(_tasks(role)).get(workload) is not None, (
            f"{role} no longer waits for {workload} before using it. k8s/manifests queues the "
            "rollout for the end of the batch, so without this the role runs against the pod "
            "it just restarted."
        )


def test_the_gate_precedes_everything_that_uses_the_workload() -> None:
    for role, workload in _MUST_GATE.items():
        tasks = _tasks(role)
        gate = _gate_indexes(tasks)[workload]
        users = [
            index
            for index, task in enumerate(tasks)
            if index != gate
            and workload in task.cmd
            and "rollout status" not in task.cmd
        ]
        assert users, (
            f"{role}: no task uses {workload} any more; re-check whether the gate is needed"
        )
        assert gate < min(users), (
            f"{role}: the rollout gate runs at task {gate}, after task {min(users)} which "
            f"already uses {workload}. The gate has to come first to be a gate."
        )


def test_the_gate_uses_rollout_status_not_a_readiness_wait() -> None:
    for role, workload in _MUST_GATE.items():
        tasks = _tasks(role)
        cmd = tasks[_gate_indexes(tasks)[workload]].cmd
        assert "condition=Available" not in cmd, (
            f"{role}: every Deployment here is single-replica, so the OLD pod satisfies "
            "Available and this returns instantly against a rollout that has not begun."
        )
        assert "--timeout=" in cmd, f"{role}: an ungated wait hangs the deploy: {cmd}"


def test_the_gate_is_tagged_with_what_it_protects() -> None:
    # `[deploy]` alone, not `[config, deploy]`: tags union for SELECTION but exclusion matches
    # ANY tag, so a dual-tagged gate vanishes under the documented `--skip-tags deploy` and the
    # config-only run silently loses it.
    #
    # Only the hand-written pair. The derived roles reach their gate through an include or a
    # block that carries the tag for them, so the tag is not on the task this file returns.
    for role, workload in _MUST_GATE.items():
        tasks = _tasks(role)
        gate = tasks[_gate_indexes(tasks)[workload]]
        assert gate.tags == ["deploy"], (
            f"{role}: the gate is tagged {gate.tags!r}; it protects deploy-tagged tasks "
            "and must carry exactly that tag."
        )


# ── the derived half ────────────────────────────────────────────────────────────────────────


# Derived on 2026-08-27. Named here so a derivation that silently stops matching fails loudly
# rather than passing an empty set — the failure mode recorded in
# memory/a-derivation-can-narrow-the-list-it-replaces.md, where replacing a hardcoded list by
# shape dropped an entry while reading as a widening.
#
# crowdsec is in BOTH halves, and that is the point: it is the one role whose shape the
# derivation and the hand-written pair agree on, so the two halves are checked against each
# other on every run.
_KNOWN_SELF_POD_ROLES = {
    "claude-otel",
    "crowdsec",
    "janitorr",
    "jellyfin",
    "pihole",
    "qbittorrent",
    "tdarr",
}

# Measured 2026-08-27: 30 resolved self-pod inspections across those roles. A floor far below
# the real count cannot tell "one role changed shape" from "the matcher broke and half the
# corpus stopped being read".
_MIN_SELF_POD_INSPECTIONS = 24

# Pod inspections whose target is a Jinja expression this file cannot resolve, each excused
# with the reason. An entry here is NOT "assume it is fine" — it is a claim that the ordering
# is carried by something else in the same role, written down where the next reader meets it.
_UNRESOLVED_TARGETS = {
    # Reads the OTHER instance's pod, and must run BEFORE this instance restarts — restarting
    # with no ready sibling is a LAN-wide DNS outage. Requiring a gate ahead of it would invert
    # the sequence the second Pi-hole exists to provide. The instance is chosen by a set_fact
    # conditional, which no static read of the command resolves.
    (
        "pihole",
        "Verify the sibling instance is ready before restarting {{ pihole_instance }}",
    ): "gating this would invert the sibling-first ordering it exists to enforce",
    # `pihole_pod_by_instance[item]` is a runtime map from instance name to pod name, built from
    # the register of "Find the pod backing each Pi-hole instance". That lookup IS resolved and
    # IS gated, and it precedes both of these, so the ordering these two need is already proven.
    (
        "pihole",
        "Reconcile the declared adlists and regex denies into gravity.db",
    ): "execs a pod name resolved at runtime; ordering carried by the gated lookup above it",
    (
        "pihole",
        "Rebuild gravity after a blocklist change",
    ): "execs a pod name resolved at runtime; ordering carried by the gated lookup above it",
}


def _self_pod_roles() -> dict[str, list[tuple[int, _Task, set[str]]]]:
    """role -> its inspections that name a workload the role itself renders."""
    found: dict[str, list[tuple[int, _Task, set[str]]]] = {}
    for role_dir in sorted(_K8S_ROLES.iterdir()):
        role = role_dir.name
        if not (role_dir / "tasks" / "main.yml").is_file() or not _owned(role):
            continue
        own = [
            (index, task, targets & set(_owned(role)))
            for index, task, targets in _inspections(role)
            if isinstance(targets, set) and targets & set(_owned(role))
        ]
        if own:
            found[role] = own
    return found


def test_the_derivation_still_matches_the_roles_it_was_written_for() -> None:
    derived = set(_self_pod_roles())
    missing = _KNOWN_SELF_POD_ROLES - derived
    assert not missing, (
        f"the self-pod-inspection derivation no longer matches {sorted(missing)}. Either those "
        "roles changed shape, or the matcher broke and is now guarding less than it claims — "
        "check which before editing this list."
    )
    seen = sum(len(inspections) for inspections in _self_pod_roles().values())
    assert seen >= _MIN_SELF_POD_INSPECTIONS, (
        f"only {seen} self-pod inspections found, expected at least "
        f"{_MIN_SELF_POD_INSPECTIONS} — the corpus shrank without a role dropping out of it."
    )


def test_every_role_that_inspects_its_own_pod_gates_on_its_rollout() -> None:
    for role, inspections in _self_pod_roles().items():
        gates = _gate_indexes(_tasks(role))
        for index, task, targets in inspections:
            for workload in sorted(targets):
                gate = gates.get(workload)
                assert gate is not None, (
                    f"{role}: task {index} ({task.raw_name!r}) inspects the pod of workload "
                    f"{workload!r} without ever waiting for its rollout. k8s/manifests QUEUES "
                    "the rollout for k8s/rollout-drain, so this reads the pod being replaced, "
                    "and every assertion after it describes the outgoing pod. The gate must "
                    f"name that same workload: `rollout status "
                    f"{_owned(role)[workload].lower()}/{workload}`."
                )
                assert gate < index, (
                    f"{role}: the rollout gate for {workload!r} is at task {gate}, after the "
                    f"inspection at {index} ({task.raw_name!r}). A gate that runs second is "
                    "not a gate."
                )


def test_no_pod_inspection_has_an_unexplained_target() -> None:
    """An unresolvable target is excused in writing or it fails. It is never skipped."""
    unexplained = []
    for role_dir in sorted(_K8S_ROLES.iterdir()):
        role = role_dir.name
        if not (role_dir / "tasks" / "main.yml").is_file() or not _owned(role):
            continue
        for _index, task, targets in _inspections(role):
            if (
                targets is _UNRESOLVED
                and (role, task.raw_name) not in _UNRESOLVED_TARGETS
            ):
                unexplained.append(f"{role}: {task.raw_name!r} — `{task.cmd.strip()}`")
    assert not unexplained, (
        "these tasks inspect a pod through an expression this guard cannot resolve, so nothing "
        "checks that the workload they talk to was rolled first. Resolve the target (a literal "
        "`deploy/<name>`, a label selector, or a register traced to one) or add it to "
        "_UNRESOLVED_TARGETS with the reason: " + "; ".join(unexplained)
    )


def test_the_unresolved_exceptions_are_all_still_needed() -> None:
    """A stale excuse is worse than none — it reads as coverage of a task that changed."""
    live = {
        (role_dir.name, task.raw_name)
        for role_dir in sorted(_K8S_ROLES.iterdir())
        if (role_dir / "tasks" / "main.yml").is_file() and _owned(role_dir.name)
        for _index, task, targets in _inspections(role_dir.name)
        if targets is _UNRESOLVED
    }
    stale = set(_UNRESOLVED_TARGETS) - live
    assert not stale, (
        "_UNRESOLVED_TARGETS excuses tasks that no longer exist or whose target now resolves; "
        "delete these entries so the set keeps meaning what it says: "
        + ", ".join(f"{role}/{name!r}" for role, name in sorted(stale))
    )


def test_no_role_gates_with_a_readiness_wait_on_its_own_pods() -> None:
    # The three primitives that LOOK like gates and are all satisfied by the outgoing pod.
    # janitorr shipped the middle one and read as gated for as long as nobody checked which pod
    # its proofs were describing.
    for role in _self_pod_roles():
        owned = set(_owned(role))
        for task in _tasks(role):
            if "wait --for=" not in task.cmd:
                continue
            targets = _selector_targets(role, task.cmd)
            reference = _kind_slash_name(_exec_target(_tokens(task.cmd), "wait") or "")
            named = (targets if isinstance(targets, set) else set()) | (
                {reference[1]} if reference else set()
            )
            assert not named & owned, (
                f"{role}: `{task.cmd.strip()}` is satisfied by the OLD pod — Ready and Available "
                "are both true of it until it stops, and status.phase stays Running while it is "
                "Terminating. Use `rollout status` instead."
            )
