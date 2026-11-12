#!/usr/bin/env python3
"""Reading a setup role's `tasks/` tree for the `when:` chain on each leaf task.

`role_hosts.setup_role_hosts` asks which hosts run any of a role. The answer comes from one
traversal: walk `tasks/main.yml` through its static imports and `block:` bodies, and collect
the `when:` chain on every leaf task. This module is that traversal; `role_hosts` evaluates
the chains it returns against each host's vars.

`task_chains` returns None when it finds no evidence, so the caller keeps the wider answer
rather than narrowing.
"""

import functools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/

from lib import yaml_fast

# include_tasks and import_tasks read alike here: the only place they diverge is a
# runtime-templated target (`include_tasks: "{{ var }}.yml"`), and _gates_in already skips
# that case (`"{{" in target`) rather than following it. A literal include_tasks --
# `include_tasks: install.yml` under a `when:` -- carries that when: to the file it pulls in
# the same way a static import_tasks does.
_IMPORT_KEYS = (
    "ansible.builtin.import_tasks",
    "import_tasks",
    "ansible.builtin.include_tasks",
    "include_tasks",
)

# A YAML date loads as a `date`, which plain `json.dumps` refuses.
task_text = functools.partial(json.dumps, default=str, skipkeys=True)


def task_chains(
    role_dir: Path,
    keep,
    task_file: str = "main.yml",
    inherited: tuple = (),
    importers: frozenset[str] = frozenset(),
) -> list[tuple] | None:
    """Every `when:` chain (import and block gates, then its own) on each leaf `keep` accepts.

    Reads the role's `tasks/` tree from `task_file` through its static imports and `block:`
    bodies. A leaf is a task that neither imports another file nor opens a block;
    `keep(task, task_file)` is called with the leaf and the basename of the task file it
    sits in, and every accepted leaf contributes one chain. Returns None when `task_file`
    cannot be read, so the caller falls back rather than narrows; an unreadable file
    further down the import tree contributes nothing, as before.

    `importers` holds the files on the import path down to `task_file`. A file on it is
    importing itself, and reads as an unreadable file.
    """
    if task_file in importers:
        return None
    path = role_dir / "tasks" / task_file
    try:
        tasks = _parse_tasks(path, path.stat().st_mtime_ns)
    except OSError, yaml_fast.UnparsableYaml:
        return None
    if tasks is None:
        return None
    return _gates_in(tasks, role_dir, keep, task_file, inherited, importers)


@functools.lru_cache(maxsize=256)
def _parse_tasks(path: Path, _mtime_ns: int) -> list | None:
    """`path` parsed once per content, keyed on its mtime as `setup_gates._parse_vars_file` is.

    Returns what `_task_list` returns, and `task_chains` reads its None the same way as an
    unreadable file.
    """
    return _task_list(path.read_text())


def _task_list(text: str) -> list | None:
    """`text` parsed as a task list, or None when no walk here can read it as one.

    A recursive alias is refused before any walk starts: refused part way through a walk,
    `keep` would have seen tasks whose chains never arrive.

    Raises:
        yaml_fast.UnparsableYaml: the text does not load.
    """
    tasks = yaml_fast.parse_yaml(text) or []
    if not isinstance(tasks, list):
        return None
    try:
        task_text(tasks)
    except ValueError, RecursionError:
        return None
    return tasks


def _gates_in(
    tasks,
    role_dir: Path,
    keep,
    task_file: str,
    inherited: tuple,
    importers: frozenset[str] = frozenset(),
) -> list[tuple]:
    found: list[tuple] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        chain = inherited + ((task["when"],) if "when" in task else ())
        target = next((task[k] for k in _IMPORT_KEYS if k in task), None)
        if isinstance(target, str):
            if "{{" in target:
                # A templated import target (`{{ role_path }}/../common/tasks/...`) is not
                # followed; the importing task itself counts as the leaf.
                if keep(task, task_file):
                    found.append(chain)
                continue
            imported = task_chains(
                role_dir, keep, Path(target).name, chain, importers | {task_file}
            )
            found.extend(imported or [])
        elif "block" in task:
            found.extend(
                _gates_in(
                    _block_body(task), role_dir, keep, task_file, chain, importers
                )
            )
        elif keep(task, task_file):
            found.append(chain)
    return found


def _block_body(task: dict) -> list:
    """The tasks under `task`'s `block:`, or none when its body is not a list."""
    body = task["block"]
    return body if isinstance(body, list) else []
