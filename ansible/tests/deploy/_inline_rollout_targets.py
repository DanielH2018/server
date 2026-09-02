"""Which workload a task talks to, and which workload a gate waits on.

Both resolve to a workload NAME read off the role's rendered manifests, never the directory
name: `claude-otel` renders `grafana`, `pihole` renders two. A `kubectl get pod -l ...`, an
`exec`/`logs <target>` and a `rollout status <kind>/<name>` each map to a set of names, or
to `_UNRESOLVED` when the target is a Jinja expression these sources cannot resolve. Split
from `test_inline_rollout_gates.py` on 2026-09-02; that module's docstring is the contract.
"""

from __future__ import annotations

import re
from collections import defaultdict

from _inline_rollout_tasks import _JINJA, _Task, _tasks
from _k8s_render import rendered_docs


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
