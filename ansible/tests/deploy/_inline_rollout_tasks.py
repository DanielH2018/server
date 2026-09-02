"""A role's tasks as they would run, with includes followed and loops unrolled.

The inline-rollout guard reads what a role DOES after the manifests include, so it needs the
expanded task list rather than the file: an `include_tasks` is followed into its target,
blocks are flattened, a `loop:` whose values can be read statically is unrolled, and the loop
variable in each unrolled command is substituted with its value so a target can be resolved.
The result is memoised per role. Split from `test_inline_rollout_gates.py` on 2026-09-02; that
module's docstring is the contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from _helpers import REPO


_REPO = REPO

_K8S_ROLES = _REPO / "ansible/roles/k8s"


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
