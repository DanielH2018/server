"""Every task of a setup role's task file, with the `when:` values that govern it.

`_helpers.walk_tasks` yields a block's children without the block's own `when:`, and it knows
nothing of an `import_tasks` that guards a whole file. A per-host guard sits on either
(docs/target-architecture/host/h7-delete-ansible.md, *Moving one host at a time*), so the
two-writer censuses read tasks through here. A `when:` that names no `<role>_packaged_hosts`
map is carried along and ignored by `setup_gates.packaged_guard_hosts`.
"""

from collections.abc import Iterator
from functools import cache
from pathlib import Path

from _helpers import load_tasks
from _role_census import role_task_files

NESTING = ("block", "rescue", "always")
IMPORTS = ("import_tasks", "include_tasks")


def _import_target(task: dict) -> str | None:
    """The file a task imports, or None for any other task or a name built at run time."""
    for key in IMPORTS:
        for module in (key, f"ansible.builtin.{key}"):
            value = task.get(module)
            if isinstance(value, dict):
                value = value.get("file")
            if isinstance(value, str) and "{{" not in value:
                return value
    return None


def _whens(task: dict) -> list:
    return [task["when"]] if "when" in task else []


def _walk(tasks: object, inherited: list) -> Iterator[tuple[dict, list]]:
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        whens = [*inherited, *_whens(task)]
        yield task, whens
        for key in NESTING:
            yield from _walk(task.get(key), whens)


@cache
def _importers(role: Path) -> dict[Path, list[tuple[Path, list]]]:
    """Each task file of `role` mapped to the files that import it, with the `when:` at the import."""
    found: dict[Path, list[tuple[Path, list]]] = {}
    for path in role_task_files(role):
        for task, whens in _walk(load_tasks(path), []):
            target = _import_target(task)
            if target:
                imported = (role / "tasks" / target).resolve()
                found.setdefault(imported, []).append((path.resolve(), whens))
    return found


def _inherited(role: Path, path: Path, seen: frozenset[Path] = frozenset()) -> list:
    """Every `when:` on an import chain that reaches `path`, outermost import first."""
    whens: list = []
    for importer, at_import in _importers(role).get(path, []):
        if importer not in seen:
            whens += [*_inherited(role, importer, seen | {path}), *at_import]
    return whens


def guarded_tasks(role: Path, task_file: Path) -> Iterator[tuple[dict, list]]:
    """`(task, whens)` for every task in `task_file`: its own `when:`, its blocks' and its imports'."""
    yield from _walk(load_tasks(task_file), _inherited(role, task_file.resolve()))
