"""Every mutation in `k8s/volume-revert` is guarded on `k8s_no_mutate`, and nothing reads past a guard.

The census is derived from the task files, not listed: a task mutates if its kubectl verb
writes or its module is `kubernetes.core.*`, the waits on those mutations count too, and a
task is guarded if its `when` carries the no-mutate clause. Three
guard rules have to cover every guarded task, and no unguarded task may read a register a
guarded one sets -- a skipped task still sets its register, so that read is the silent
failure the guard exists to prevent. The role also never scales a workload back up.
"""

from __future__ import annotations

import re

import yaml
from _helpers import load_tasks as _tasks
from pathlib import Path

from _volume_revert import _CLAIM, _GUARD, _MAIN, _guard_of


# kubectl verbs that change the cluster. `get`, `describe` and friends are deliberately absent:
# a read is what `check_mode: false` exists to let run under `--check`.
_WRITE_VERBS = {
    "scale",
    "patch",
    "apply",
    "create",
    "delete",
    "replace",
    "edit",
    "annotate",
    "label",
    "cordon",
    "uncordon",
    "drain",
    "taint",
    "exec",
    "cp",
    "rollout",
}


def _role_tasks() -> list[tuple[Path, dict]]:
    """Every task in the role, both files.

    Both files, because `main.yml` is where someone writes "and bring it back up" — and a census
    that reads only `claim.yml` cannot see it. Measured 2026-08-21: an unguarded
    `--replicas=1` appended to `main.yml` left all 27 tests green.
    """
    return [(path, task) for path in (_CLAIM, _MAIN) for task in _tasks(path)]


def _mutating_tasks() -> list[tuple[Path, dict]]:
    """Tasks that change the cluster, or that wait on a change this role made.

    The waits are included deliberately: under `k8s_no_mutate` the scale-down and the attach
    never happen, so an unguarded wait polls for a transition nobody requested and burns its
    whole timeout before failing a dry run that changed nothing.

    Recognises a write by its kubectl verb rather than by the one verb this role happens to use
    today, and recognises `kubernetes.core.*` as mutating whatever the module. The previous
    version saw only `uri`, `scale` and `until`; an appended `kubectl patch pvc/...` was
    invisible to it.
    """
    out = []
    for path, task in _role_tasks():
        modules = [
            key
            for key in task
            if "." in key and not key.startswith("ansible.builtin.set")
        ]
        if any(module.startswith("kubernetes.core.") for module in modules):
            out.append((path, task))
            continue
        if "ansible.builtin.uri" in task:
            out.append((path, task))
            continue
        command = task.get("ansible.builtin.command") or task.get(
            "ansible.builtin.shell"
        )
        if not isinstance(command, dict):
            continue
        argv = [str(token) for token in command.get("argv", [])]
        if _WRITE_VERBS.intersection(argv) or "until" in task:
            out.append((path, task))
    return out


def _is_guarded(task: dict) -> bool:
    return _GUARD in _guard_of(task)


_PROSE_KEYS = {"name", "msg", "fail_msg", "success_msg"}


def _strip_prose(node):
    """Drop the human-readable fields, at any depth.

    Everything left is something Ansible acts on. The prose is dropped because it legitimately
    mentions the very strings the checks below hunt for — the frontend assert's failure message
    says the workload "is at zero replicas", and an operator reading it mid-incident needs that
    sentence more than a scanner needs a simpler rule.
    """
    if isinstance(node, dict):
        return {k: _strip_prose(v) for k, v in node.items() if k not in _PROSE_KEYS}
    if isinstance(node, list):
        return [_strip_prose(item) for item in node]
    return node


def _body(task: dict) -> str:
    """The task as YAML, minus the prose fields."""
    return yaml.safe_dump(_strip_prose(task))


def _body_with_prose(task: dict) -> str:
    """The task as YAML, minus its name only.

    The register check below needs the messages: a `debug` whose `msg` templates a skipped
    task's register fails the dry run exactly like a `when` that reads one.
    """
    return yaml.safe_dump({key: value for key, value in task.items() if key != "name"})


def test_the_role_never_scales_back_up() -> None:
    """Every one of the thirteen manifests carries an explicit `replicas: 1`, so the apply that
    follows this role restores the Deployment. Scaling back here would roll the workload twice
    and race the apply.

    Both files, and the class rather than the literal. Measured 2026-08-21: appending an
    unguarded `kubectl scale ... --replicas=1` to `main.yml` left all 27 tests green, because
    the check read `claim.yml` alone; and a scale-back can equally be written
    `--replicas={{ n }}`, `kubernetes.core.k8s_scale`, or `kubectl patch -p '{"spec":
    {"replicas":1}}'`. So: the only replica count this role may name anywhere is zero.
    """
    for _path, task in _role_tasks():
        body = _body(task)
        assert "k8s_scale" not in body, task["name"]
        for match in re.finditer(r"replicas", body):
            # `=0` and then a non-digit, not merely the two characters `=0`. Measured
            # 2026-08-21: a decoy `--replicas=01` — a scale to ONE, spelled to look like zero —
            # satisfied the two-character check and passed. Nothing else caught it either,
            # except the pinned census count, and that stops helping the moment someone
            # legitimately adds an eighth mutating task and bumps the number.
            assert re.match(r"=0(?![0-9])", body[match.end() :]), (
                f"{task['name']!r} names a replica count that is not zero: "
                f"...{body[match.start() - 20 : match.end() + 20]}... The apply that follows a "
                f"revert restores the Deployment; this role only ever scales to zero."
            )
        argv = [
            str(token)
            for token in (task.get("ansible.builtin.command") or {}).get("argv", [])
        ]
        if "scale" in argv:
            assert "to zero replicas" in task["name"], task["name"]


def test_every_mutation_is_guarded_on_k8s_no_mutate() -> None:
    """`k8s_no_mutate` is `ansible_check_mode or (k8s_dry_run | bool)`. Guarding on either half
    alone leaves the other half mutating a live cluster during a run that promised not to.

    WHICH TEST COVERS WHICH GUARD. Nine tasks in `claim.yml` carry the guard, and three
    mechanisms divide them — jointly exhaustive as of 2026-09-01, and nothing makes them stay
    that way:

      * seven by this census — the scale-down, the three waits and the three API calls. It was
        eight until 2026-09-01, when the seeded-annotation strip went: it paired with
        k8s/volume-claim's short-circuit, and that short-circuit no longer exists;
      * one by `test_nothing_unguarded_reads_a_guarded_tasks_output` — the frontend assert,
        caught through its read of `volume_revert_attached` rather than as a mutation;
      * one by two dedicated tests — `Fail when no snapshot matches this deploy`, which is a
        `fail` with no register and no kubectl verb, so BOTH generic rules are blind to it.

    An eleventh guarded task is therefore not automatically covered. Work out which of the three
    would notice it, and if the answer is none, write the test that does.
    """
    mutating = _mutating_tasks()
    # An exact count, not a floor. The census recognises a write by its kubectl verb, every
    # `kubernetes.core.*` module and every polling wait — but a task shaped like none of those
    # would still be invisible, and a floor would let it arrive unguarded with this test green.
    # Pinning the number makes whoever adds a task read this comment.
    assert len(mutating) == 7, (
        f"the mutating-task census found {len(mutating)} tasks, not 8. If you added a mutation, "
        f"guard it and update this count; if the census stopped recognising one, fix "
        f"_mutating_tasks — a task it cannot see is a task this test does not check."
    )
    for _path, task in mutating:
        assert _is_guarded(task), task["name"]


def test_the_three_guard_rules_cover_every_guarded_task() -> None:
    """The arithmetic in the census docstring, made executable.

    Each guarded task must be caught by at least one of the three mechanisms. A tenth guarded
    task shaped like none of them — another `fail`, a `debug`, a `wait_for` — would otherwise
    sit there with its guard checked by nothing, which is precisely how a guard gets dropped in
    a later edit and noticed by no test.
    """
    guarded_outputs = _guarded_outputs()
    # By name, not identity: every helper re-parses the YAML, so the same task is a different
    # dict object each call.
    census = {task["name"] for _path, task in _mutating_tasks()}
    uncovered = []
    for path, task in _role_tasks():
        if path != _CLAIM or not _is_guarded(task):
            continue
        by_census = task["name"] in census
        own = set(task.get("ansible.builtin.set_fact") or {}) | {task.get("register")}
        body = _body_with_prose(task)
        by_output = any(name in body for name in guarded_outputs - own)
        by_dedicated = "Fail when no snapshot matches this deploy" in task["name"]
        if not (by_census or by_output or by_dedicated):
            uncovered.append(task["name"])
    assert not uncovered, (
        f"these guarded tasks are covered by no rule: {uncovered}. The census sees writes and "
        f"waits, the output rule sees consumers of a guarded task's register or set_fact, and "
        f"the missing-snapshot failure has two tests of its own. Yours matches none — write "
        f"the test that would notice its guard disappearing."
    )


def _guarded_outputs() -> set[str]:
    """Names a `k8s_no_mutate`-guarded task produces: its `register`, and its `set_fact` keys."""
    outputs = set()
    for _path, task in _role_tasks():
        if not _is_guarded(task):
            continue
        if "register" in task:
            outputs.add(task["register"])
        outputs.update(task.get("ansible.builtin.set_fact") or {})
    return outputs


def test_nothing_unguarded_reads_a_guarded_tasks_output() -> None:
    """A task that consumes a guarded task's output must carry the same guard.

    Under `--check` the guarded task is skipped, its output is undefined, and the consumer
    fails the dry run — a run that promised to change nothing instead changes nothing and dies.
    Measured 2026-08-21: deleting the guard from the frontend assert, whose `that` reads
    `volume_revert_attached`, left all 27 tests green, because an `assert` is not a mutation.

    Output means `register` AND `set_fact`. A guarded `set_fact` is skipped exactly like a
    guarded command, and its keys are undefined for the same reason — `volume_revert_snapshot`
    is that shape today, produced by a `set_fact` and read by the guarded revert. No guarded
    `set_fact` exists as of 2026-08-21, so this half of the rule currently names nothing; it is
    here so the next one is covered by the rule rather than by whoever writes it being careful.
    """
    guarded_outputs = _guarded_outputs()
    assert guarded_outputs, "no guarded task produces anything; this test reads nothing"
    for _path, task in _role_tasks():
        if _is_guarded(task):
            continue
        body = _body_with_prose(task)
        for name in guarded_outputs:
            assert name not in body, (
                f"{task['name']!r} reads {name!r}, which a `{_GUARD}`-guarded task produces, "
                f"but carries no such guard of its own."
            )
