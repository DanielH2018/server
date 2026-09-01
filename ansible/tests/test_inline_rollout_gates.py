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

  * Roles that render no Deployment/DaemonSet/StatefulSet (seed-volume, netpol-baseline,
    rollout-drain, media-volume, ...). They have no rollout of their own to gate on, and every
    pod they touch belongs to someone else.
  * `logs job/<name>` and other Job targets. A Job has no rollout for `rollout status` to gate.
  * Reaching a workload over its Service instead of `kubectl` — registry dials `deploy/registry`
    from three self-test Jobs, which no command text here can see. That is why registry stays
    in the hand-written `_MUST_GATE` and cannot be derived.
  * `when:` conditions. A gate that is skipped at runtime still counts as a gate here. pihole's
    is conditional on the manifests having changed, which is also the only run that rolls it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml
from _k8s_render import rendered_docs
from _helpers import REPO

_REPO = REPO
_K8S_ROLES = _REPO / "ansible/roles/k8s"

# role -> the workload its own tasks talk to after the manifests include.
_MUST_GATE = {
    "crowdsec": "crowdsec",
    "registry": "registry",
}

# The kinds `rollout status` can gate. A Job or a bare Pod is neither rolled nor gateable.
_WORKLOAD_KINDS = {"Deployment", "DaemonSet", "StatefulSet"}

_KIND_ALIASES = {
    "deploy": "Deployment",
    "deployment": "Deployment",
    "deployments": "Deployment",
    "ds": "DaemonSet",
    "daemonset": "DaemonSet",
    "daemonsets": "DaemonSet",
    "sts": "StatefulSet",
    "statefulset": "StatefulSet",
    "statefulsets": "StatefulSet",
}

# Returned instead of a target set when a command names its target through a Jinja expression
# that cannot be resolved from the role's own sources.
_UNRESOLVED = object()


# ── what each role renders ──────────────────────────────────────────────────────────────────


_INDEX: tuple[dict, dict] | None = None


def _index() -> tuple[
    dict[str, dict[str, str]], dict[str, dict[tuple[str, str], set[str]]]
]:
    """(role -> {workload name: kind}, role -> {(label key, value): {workload names}}).

    Built from the RENDERED manifests, so a name that is a Jinja expression in the template
    still resolves. The label index is what turns pihole's `-l instance=pihole-2` into the
    workload `pihole-2` without this file knowing anything about pihole.
    """
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    owned: dict[str, dict[str, str]] = defaultdict(dict)
    by_label: dict[str, dict[tuple[str, str], set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") not in _WORKLOAD_KINDS:
            continue
        name = (doc.get("metadata") or {}).get("name")
        if not name:
            continue
        owned[role][name] = doc["kind"]
        labels = (
            ((doc.get("spec") or {}).get("template") or {}).get("metadata") or {}
        ).get("labels") or {}
        for key, value in labels.items():
            by_label[role][(str(key), str(value))].add(name)
    _INDEX = (dict(owned), dict(by_label))
    return _INDEX


def _owned(role: str) -> dict[str, str]:
    return _index()[0].get(role, {})


def _workloads_with_label(role: str, key: str, value: str) -> set[str]:
    return set(_index()[1].get(role, {}).get((key, value), set()))


# ── the role's tasks, flattened and loop-expanded ───────────────────────────────────────────


@dataclass
class _Task:
    raw_name: str  # as written, still templated — the key _UNRESOLVED_TARGETS uses
    cmd: str  # loop variables substituted
    tags: object
    register: str | None


_JINJA = re.compile(r"\{\{\s*(.*?)\s*\}\}")


def _load(path: Path) -> list:
    return yaml.safe_load(path.read_text()) or []


def _role_vars(role: str) -> dict:
    merged: dict = {}
    for relative in ("defaults/main.yml", "vars/main.yml"):
        path = _K8S_ROLES / role / relative
        if path.is_file():
            data = yaml.safe_load(path.read_text())
            if isinstance(data, dict):
                merged.update(data)
    return merged


def _cmd(task: dict) -> str:
    for key in (
        "ansible.builtin.command",
        "ansible.builtin.shell",
        "command",
        "shell",
    ):
        module = task.get(key)
        if isinstance(module, dict):
            return str(module.get("cmd", ""))
        if isinstance(module, str):
            return module
    return ""


def _substitute(text: str, var: str, value) -> str:
    """Replace `{{ var }}` and `{{ var.key }}` with the loop value bound to them."""

    def replace(match: re.Match) -> str:
        expr = match.group(1)
        if expr == var and not isinstance(value, dict):
            return str(value)
        attribute = re.fullmatch(rf"{re.escape(var)}\.(\w+)", expr)
        if attribute and isinstance(value, dict) and attribute.group(1) in value:
            return str(value[attribute.group(1)])
        return match.group(0)

    return _JINJA.sub(replace, text)


def _render(text: str, bindings: dict) -> str:
    for var, value in bindings.items():
        text = _substitute(text, var, value)
    return text


def _loop_of(task: dict, role: str) -> tuple[bool, list | None]:
    """(has a loop, its values or None when they cannot be read statically).

    `loop: "{{ some_role_var }}"` is resolved from the role's own defaults/vars — that is how
    claude-otel's six telemetry workloads become six tasks. A loop over a play-scoped
    accumulator (`k8s_pending_rollouts`) resolves to nothing, and the tasks under it keep their
    `{{ item }}` and are judged unresolved rather than guessed at.
    """
    loop = task.get("loop", task.get("with_items"))
    if loop is None:
        return False, None
    if isinstance(loop, str):
        name = re.fullmatch(r"\s*\{\{\s*(\w+)\s*\}\}\s*", loop)
        if not name:
            return True, None
        resolved = _role_vars(role).get(name.group(1))
        return True, resolved if isinstance(resolved, list) else None
    if isinstance(loop, list):
        return True, loop
    return True, None


def _include_target(task: dict, tasks_dir: Path) -> Path | None:
    """The file an include/import names, when it names one literally.

    BOTH `include_tasks` AND `import_tasks` are followed. Reading only the first missed
    janitorr, which imports. The two forms differ in WHEN Ansible resolves the file, not in
    whether those tasks run, so a guard reading for shape has to treat them alike. A templated
    include is not resolvable here and is left as the opaque task it is.
    """
    include = next(
        (
            task[key]
            for key in (
                "ansible.builtin.include_tasks",
                "ansible.builtin.import_tasks",
                "include_tasks",
                "import_tasks",
            )
            if key in task
        ),
        None,
    )
    if include is None:
        return None
    name = include.get("file") if isinstance(include, dict) else include
    if not isinstance(name, str) or "{{" in name:
        return None
    target = tasks_dir / name
    return target if target.is_file() else None


def _expand(role: str, entries: list, tasks_dir: Path, bindings: dict) -> list[_Task]:
    out: list[_Task] = []
    for task in entries:
        if not isinstance(task, dict):
            continue
        has_loop, values = _loop_of(task, role)
        loop_var = (task.get("loop_control") or {}).get("loop_var", "item")
        if has_loop and values is not None:
            environments = [{**bindings, loop_var: value} for value in values]
        else:
            environments = [dict(bindings)]
        for environment in environments:
            out.extend(_emit(role, task, tasks_dir, environment))
    return out


def _emit(role: str, task: dict, tasks_dir: Path, bindings: dict) -> list[_Task]:
    nested = [
        task[key]
        for key in ("block", "rescue", "always")
        if isinstance(task.get(key), list)
    ]
    if nested:
        out: list[_Task] = []
        for entries in nested:
            out.extend(_expand(role, entries, tasks_dir, bindings))
        return out
    include = _include_target(task, tasks_dir)
    if include is not None:
        return _expand(role, _load(include), tasks_dir, bindings)
    return [
        _Task(
            raw_name=str(task.get("name", "")),
            cmd=_render(_cmd(task), bindings),
            tags=task.get("tags"),
            register=task.get("register"),
        )
    ]


_TASKS_CACHE: dict[str, list[_Task]] = {}


def _tasks(role: str) -> list[_Task]:
    """main.yml with includes inlined, blocks flattened and literal loops expanded.

    ORDER SURVIVES ALL THREE, which is the only reason this file can say "the gate runs first".
    Blocks matter as much as includes: pihole's `rollout status` sits inside a block inside a
    looped `include_tasks`, three levels down from main.yml, and a guard reading main.yml alone
    sees none of it.
    """
    if role not in _TASKS_CACHE:
        tasks_dir = _K8S_ROLES / role / "tasks"
        _TASKS_CACHE[role] = _expand(role, _load(tasks_dir / "main.yml"), tasks_dir, {})
    return _TASKS_CACHE[role]


# ── reading a command's workload ────────────────────────────────────────────────────────────


def _tokens(cmd: str) -> list[str]:
    """Whitespace tokens, with each `{{ ... }}` collapsed so it survives as ONE token."""
    return _JINJA.sub(lambda m: "{{" + m.group(1).replace(" ", "") + "}}", cmd).split()


def _kind_slash_name(token: str) -> tuple[str, str] | None:
    if "/" not in token:
        return None
    kind, _, name = token.partition("/")
    return (_KIND_ALIASES.get(kind.lower(), ""), name)


def _gate_target(cmd: str) -> str | None:
    """The workload a `rollout status` waits for, or None if this is not one we can read."""
    if "rollout status" not in cmd:
        return None
    tokens = _tokens(cmd)
    index = next(
        (
            i
            for i in range(len(tokens) - 1)
            if tokens[i : i + 2] == ["rollout", "status"]
        ),
        None,
    )
    if index is None:
        return None
    for token in tokens[index + 2 :]:
        if token.startswith("-"):
            continue
        reference = _kind_slash_name(token)
        if reference and reference[0] in _WORKLOAD_KINDS and "{{" not in reference[1]:
            return reference[1]
        return None
    return None


def _gate_indexes(tasks: list[_Task]) -> dict[str, int]:
    """workload -> index of the FIRST task that waits for its rollout."""
    gates: dict[str, int] = {}
    for index, task in enumerate(tasks):
        workload = _gate_target(task.cmd)
        if workload is not None:
            gates.setdefault(workload, index)
    return gates


_EXEC_FLAGS_WITH_VALUE = {"-c", "-n", "--container", "--namespace", "--context"}


def _exec_target(tokens: list[str], verb: str) -> str | None:
    """The object token an `exec`/`logs` acts on, skipping kubectl's flags."""
    try:
        start = tokens.index(verb) + 1
    except ValueError:
        return None
    skip_next = False
    for token in tokens[start:]:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            return None
        if token in _EXEC_FLAGS_WITH_VALUE:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def _selector_targets(role: str, cmd: str) -> set[str] | object | None:
    match = re.search(r"(?:^|\s)(?:-l|--selector=?)\s*(\S+)", cmd)
    if not match:
        return None
    selector = _JINJA.sub(
        lambda m: "{{" + m.group(1).replace(" ", "") + "}}", match.group(1)
    )
    if "=" not in selector:
        # `-l netpol-baseline-exempt` is a key-existence selector: it names no workload.
        return None
    key, _, value = selector.partition("=")
    if "{{" in key or "{{" in value:
        return _UNRESOLVED
    return _workloads_with_label(role, key, value)


def _pod_targets(
    role: str, task: _Task, registers: dict[str, set[str] | object]
) -> set[str] | object | None:
    """The workloads a task's command inspects a pod of.

    None  — not a pod inspection at all, or one aimed at something with no rollout (a Job).
    set() — resolved, and names no workload THIS role renders (janitorr reading jellyfin's pod).
    _UNRESOLVED — a pod inspection whose target this file cannot resolve statically.
    """
    cmd = task.cmd
    if "kubectl" not in cmd:
        return None
    tokens = _tokens(cmd)
    if re.search(r"(?:^|\s)get\s+pods?(?:\s|$)", cmd):
        return _selector_targets(role, cmd)
    for verb in ("exec", "logs"):
        if verb not in tokens:
            continue
        target = _exec_target(tokens, verb)
        if target is None:
            return _UNRESOLVED
        reference = _kind_slash_name(target)
        if reference is not None:
            if reference[0] not in _WORKLOAD_KINDS:
                # job/<name>, cronjob/<name>: nothing for `rollout status` to gate.
                return None
            if "{{" in reference[1]:
                return _UNRESOLVED
            return {reference[1]} & set(_owned(role))
        if "{{" in target:
            traced = re.fullmatch(r"\{\{(\w+)\.stdout\}\}", target)
            if traced and traced.group(1) in registers:
                return registers[traced.group(1)]
            return _UNRESOLVED
        # A literal bare pod name. Pod names carry a ReplicaSet suffix, so one that equals a
        # workload name is a bare Pod (seed-<claim>), not a rolled workload.
        return set()
    return None


def _inspections(role: str) -> list[tuple[int, _Task, set[str] | object]]:
    """(index, task, targets) for every pod inspection in the role, in play order.

    The register map is built as the walk goes, so `exec {{ jellyfin_k8s_pod.stdout }}` inherits
    the targets of the `get pod -l app=jellyfin` that filled it. A register produced by a task
    whose own target was unresolved stays unresolved — it is never demoted to "no targets",
    which would pass silently.
    """
    registers: dict[str, set[str] | object] = {}
    found = []
    for index, task in enumerate(_tasks(role)):
        targets = _pod_targets(role, task, registers)
        if targets is None:
            continue
        if task.register:
            registers[task.register] = targets
        found.append((index, task, targets))
    return found


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
