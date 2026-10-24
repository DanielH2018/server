"""The items the setup roles still own, derived from the tree (H7.2f).

`docs/target-architecture/host/h7-delete-ansible.md`, *The component checklist and the setup
census*, has the design. A hand list of "what is left to move" goes stale the first time a task
is added, so this module reads three sources for every setup role and host:

- the crons and timers the role's generated `## At a glance` block lists;
- every task under its `tasks/`, whatever its module, except the control and read-only modules
  in `CONTROL_MODULES`. A `command` or `shell` task with `changed_when: false` and no
  `creates:` or `removes:` is a read, so it is not an item;
- each `import_tasks` of a `common/tasks/` file.

A task counts against its `delegate_to:` host when it names one, and otherwise against the
hosts the role's play reaches. A `delegate_to:` on an enclosing `block:` applies to every task
in it. A role whose leading `meta: end_role` guard ends it on a host reaches no host there, so
an emptied role's stub task is a control module and yields no item.

The census does not evaluate a task's `when:`. A per-host guard such as `when: inventory_hostname
not in <role>_packaged_hosts['x']` therefore cannot hide an item: the task still counts on a host
the map names, and stays in the report until that host's package carries what the task writes.
The report marks a task whose own `when:` names the map. H7.21 adds the guard evaluation to the
two-writer censuses; this census needs none, because it asks the package, not the guard.

`setup_census.py` resolves each item against the host packages and prints what is left.
"""

import re
import sys as _sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import jinja2

from docs.catalog_lib.glance_facts import cron_jobs, timer_units
from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from lib.k8s_roles import role_dirs
from lib.render_context import render_context
from lib.repo_paths import REPO, ROLES

SETUP_ROLES = ROLES / "setup"

# The modules whose tasks are not items: they decide, read or fail, and write nothing a package
# could own. `import_tasks` and `include_tasks` are followed through the task files themselves.
CONTROL_MODULES = frozenset(
    {
        "stat",
        "assert",
        "debug",
        "set_fact",
        "slurp",
        "find",
        "getent",
        "fail",
        "meta",
        "wait_for",
        "service_facts",
        "docker_host_info",
        "import_tasks",
        "include_tasks",
    }
)
_COMMANDS = frozenset({"command", "shell"})
# The keys of a task that are not its module.
_DIRECTIVES = frozenset(
    {
        "name",
        "when",
        "tags",
        "register",
        "loop",
        "with_items",
        "with_dict",
        "with_fileglob",
        "become",
        "become_user",
        "become_method",
        "delegate_to",
        "vars",
        "notify",
        "changed_when",
        "failed_when",
        "no_log",
        "check_mode",
        "ignore_errors",
        "ignore_unreachable",
        "retries",
        "delay",
        "until",
        "environment",
        "run_once",
        "args",
        "block",
        "rescue",
        "always",
        "listen",
        "loop_control",
        "throttle",
        "any_errors_fatal",
        "diff",
        "timeout",
        "module_defaults",
    }
)
# Where each module names the thing it manages, first present wins.
_TARGET_KEYS = ("dest", "path", "name", "pkg", "key", "rule", "port", "repo")
_ONE_VARIABLE = re.compile(r"^\{\{\s*([A-Za-z_][\w.]*)\s*\}\}$")
_ENV = make_ansible_env(undefined_cls=jinja2.StrictUndefined)


@dataclass(frozen=True)
class Item:
    """One thing a setup role does on a host, which a package or a declaration must replace.

    Attributes:
        host: The host the item counts against.
        role: The setup role that owns it.
        kind: The module's short name, `cron` or `timer` for the At a glance entries, or
            `import` for an import of a `common/tasks/` file.
        name: The task's name, the cron's name, the timer's unit, or the imported file.
        target: What the item manages, rendered for the host when its template resolves.
        src: A `src:` file or template the task names, or an empty string.
        source: The repo-relative file the item comes from.
        absent: Whether the task removes its target (`state: absent`).
        guarded: Whether the task's own `when:` names a `<role>_packaged_hosts` map.
    """

    host: str
    role: str
    kind: str
    name: str
    target: str = ""
    src: str = ""
    source: str = ""
    absent: bool = False
    guarded: bool = False

    def line(self) -> str:
        """The one line the report prints."""
        shown = f" [{self.target}]" if self.target else ""
        shipped = f" <- {self.src}" if self.src else ""
        guard = " [per-host guard: no package carries it]" if self.guarded else ""
        return f"{self.role} | {self.kind} | {self.name}{shown}{shipped}{guard} ({self.source})"

    def mentions(self, text: str) -> bool:
        """Whether `text` appears in any of the item's identifying fields."""
        return any(
            text in field for field in (self.name, self.target, self.src, self.source)
        )


def short_module(key: str) -> str:
    """`ansible.posix.sysctl` -> `sysctl`."""
    return key.rsplit(".", 1)[-1]


def task_module(task: dict) -> str | None:
    """The key of `task` that names its module, or None for a wrapper or an empty task."""
    for key in task:
        if key not in _DIRECTIVES:
            return key
    return None


def is_read(task: dict, module: str) -> bool:
    """A `command` or `shell` task that only reads: `changed_when: false`, no `creates:`."""
    if short_module(module) not in _COMMANDS:
        return False
    if str(task.get("changed_when")).lower() != "false":
        return False
    return not ({"creates", "removes"} & set(_fields(task, module)))


def walk(
    tasks: object, delegate: str | None = None
) -> Iterator[tuple[dict, str | None]]:
    """Each leaf task with the `delegate_to` of the nearest enclosing block that names one."""
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        here = task.get("delegate_to", delegate)
        if "block" in task:
            for key in ("block", "rescue", "always"):
                yield from walk(task.get(key), here)
            continue
        yield task, here


def _render(value: object, ctx: dict) -> str:
    """`value` rendered with `ctx`, or its template text when a name does not resolve."""
    text = str(value)
    try:
        return _ENV.from_string(text).render(ctx)
    except jinja2.TemplateError:
        return text


def _loop_items(task: dict, ctx: dict) -> list:
    """The items a task's loop runs over, `[None]` for no loop or an unreadable one."""
    loop = task.get("loop", task.get("with_items"))
    if isinstance(loop, list):
        return loop
    match = _ONE_VARIABLE.match(str(loop)) if loop is not None else None
    if match:
        try:
            found = _ENV.compile_expression(match.group(1))(**ctx)
        except jinja2.TemplateError:
            return [None]
        if isinstance(found, list):
            return found
    return [None]


def _fields(task: dict, module: str) -> dict:
    args = task[module] if isinstance(task[module], dict) else {}
    extra = task["args"] if isinstance(task.get("args"), dict) else {}
    return {**args, **extra}


def task_items(
    task: dict,
    role: str,
    host: str,
    source: str,
    ctx: dict,
    control: frozenset[str] = CONTROL_MODULES,
) -> list[Item]:
    """The items one task yields for `host`, one per loop item.

    `control` is the set of modules that yield nothing. A caller passes a narrower set only to
    prove the census notices when a module drops out of it.
    """
    module = task_module(task)
    if module is None or short_module(module) in control:
        if module and short_module(module) == "import_tasks":
            target = str(task[module])
            if "common/tasks/" in target:
                return [Item(host, role, "import", file_name(target), source=source)]
        return []
    if is_read(task, module):
        return []
    fields = _fields(task, module)
    found = []
    for loop_item in _loop_items(task, ctx):
        item_ctx = ctx if loop_item is None else {**ctx, "item": loop_item}
        target = next(
            (_render(fields[k], item_ctx) for k in _TARGET_KEYS if k in fields), ""
        )
        name = _render(task.get("name", module), item_ctx)
        if short_module(module) == "cron":
            # A cron is named by its own `name:`, the way the At a glance entry names it, so the
            # two collapse into one item.
            name = _render(fields.get("name", name), item_ctx)
            target = ""
        found.append(
            Item(
                host=host,
                role=role,
                kind=short_module(module),
                name=name,
                target=target,
                src=_render(fields["src"], item_ctx) if "src" in fields else "",
                source=source,
                absent=_render(fields.get("state", ""), item_ctx) == "absent",
                guarded="_packaged_hosts" in str(task.get("when", "")),
            )
        )
    return found


def file_name(target: str) -> str:
    """The file name an `import_tasks` value ends in."""
    return target.rstrip("/").rsplit("/", 1)[-1]


def glance_items(role_dir: _Path, host: str, source: str) -> list[Item]:
    """The role's At a glance crons and timers, as items."""
    crons = [
        Item(host, role_dir.name, "cron", name, source=source)
        for name, _ in cron_jobs(role_dir)
    ]
    timers = [
        Item(host, role_dir.name, "timer", unit, source=source)
        for unit, _ in timer_units(role_dir)
    ]
    return crons + timers


def role_items(
    role_dir: _Path,
    hosts: tuple[str, ...],
    reach: frozenset[str],
    ctx_of: Callable[[_Path, str], dict] = render_context,
    repo: _Path = REPO,
    control: frozenset[str] = CONTROL_MODULES,
) -> list[Item]:
    """Every item `role_dir` yields on `hosts`, de-duplicated by identity."""
    found: dict[tuple, Item] = {}
    for host in sorted(reach & set(hosts)):
        for item in glance_items(
            role_dir, host, str(role_dir.relative_to(repo) / "CLAUDE.md")
        ):
            found.setdefault((item.host, item.kind, item.name, item.target), item)
    for task_file in sorted((role_dir / "tasks").rglob("*.yml")):
        source = str(task_file.relative_to(repo))
        tasks = yaml_fast.safe_load(task_file.read_text())
        for task, delegate in walk(tasks):
            targets = [delegate] if delegate in hosts else sorted(reach & set(hosts))
            for host in targets:
                for item in task_items(
                    task, role_dir.name, host, source, ctx_of(role_dir, host), control
                ):
                    found.setdefault(
                        (item.host, item.kind, item.name, item.target), item
                    )
    return list(found.values())


def setup_roles(setup: _Path = SETUP_ROLES) -> list[_Path]:
    """Every setup role directory, `common` included, a retired role's debris skipped."""
    return role_dirs(setup)
