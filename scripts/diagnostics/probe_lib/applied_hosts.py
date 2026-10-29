"""Which task files of a setup role `probe.py applied` walks on each host.

The first playbook in `PLAYBOOKS` that applies the role decides, and every play in it that
applies the role counts. A play reaches the role in one of two ways:

- a `roles:` entry, which runs `tasks/main.yml` on each host its `when:` admits and the role's
  leading `end_role` guard keeps;
- an `include_role` or `import_role` task naming the role, which runs `tasks/<tasks_from>.yml`
  on each host its `when:` admits.

A play's `assert` pre_tasks also bound its hosts, because a host that fails one stops the play
before the role runs. `k3s-bringup.yml`'s server play asserts `inventory_hostname in
k3s_server_hosts`, and its join play asserts `inventory_hostname == 'daniel-server'`, so the
server's crons are walked on daniel-box only and the agent's `tasks/agent.yml` on
daniel-server only (#6475).
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dataclasses import dataclass
from pathlib import Path

from deploy_tools.setup_lib.setup_gates import (
    HOSTS,
    GateUnreadable,
    eval_when_strict,
    role_end_hosts,
)
from diagnostics.probe_lib import core
from lib import yaml_fast
from lib.repo_paths import ANSIBLE

# The playbooks that apply a setup role, in `setup_routing.PLAYBOOKS`' precedence order.
PLAYBOOKS = ("initial_setup.yml", "k3s-bringup.yml", "bootstrap.yml")
_ROLE_TASKS = tuple(
    f"{prefix}{kind}"
    for kind in ("include_role", "import_role")
    for prefix in ("", "ansible.builtin.")
)
_ASSERTS = ("assert", "ansible.builtin.assert")


@dataclass(frozen=True)
class Entry:
    """One place a playbook runs the role.

    Attributes:
        tasks_from: the task file it runs, `main` for a `roles:` entry.
        when: the entry's own `when:`, or None.
        asserts: the `that:` conditions of the play's `assert` pre_tasks.
    """

    tasks_from: str
    when: object
    asserts: tuple


def _asserts(play: dict) -> tuple:
    found = []
    for task in play.get("pre_tasks") or []:
        args = next(
            (task[k] for k in _ASSERTS if isinstance(task, dict) and k in task), None
        )
        that = (args or {}).get("that") if isinstance(args, dict) else None
        found += that if isinstance(that, list) else [that] if that else []
    return tuple(found)


def entries(role: str, ansible: Path = ANSIBLE) -> list[Entry]:
    """Every place the first playbook that applies `role` runs it.

    Raises:
        core.UsageError: no playbook applies the role.
    """
    for name in PLAYBOOKS:
        path = ansible / name
        found = []
        for play in (
            yaml_fast.safe_load(path.read_text()) if path.is_file() else []
        ) or []:
            play = play or {}
            asserts = _asserts(play)
            for entry in play.get("roles") or []:
                if entry == role:
                    found.append(Entry("main", None, asserts))
                elif isinstance(entry, dict) and entry.get("role") == role:
                    found.append(Entry("main", entry.get("when"), asserts))
            for task in play.get("tasks") or []:
                args = next(
                    (
                        task[k]
                        for k in _ROLE_TASKS
                        if isinstance(task, dict) and k in task
                    ),
                    None,
                )
                if isinstance(args, dict) and args.get("name") == role:
                    tasks_from = str(args.get("tasks_from") or "main")
                    found.append(Entry(tasks_from, task.get("when"), asserts))
        if found:
            return found
    raise core.UsageError(
        f"applied: no playbook in {', '.join(PLAYBOOKS)} applies {role}"
    )


def _admits(entry: Entry, host: str, forced: bool) -> bool:
    """Whether `entry` runs on `host`.

    Raises:
        core.UsageError: the entry's `when:` cannot be read for `host` and `forced` is False.
    """
    # DECIDED: an assert the probe cannot read admits the host, the wide reading
    # `role_end_hosts` gives an unreadable guard: dropping a host on a guess hides its files.
    for that in entry.asserts:
        try:
            if not eval_when_strict(that, host):
                return False
        except GateUnreadable:
            continue
    if entry.when is None:
        return True
    try:
        return eval_when_strict(entry.when, host)
    except GateUnreadable as exc:
        if forced:
            return True
        raise core.UsageError(
            f"applied: a gate on {entry.tasks_from}: {exc}; pass --host"
        ) from exc


def task_files(
    role: str, role_dir: Path, host: str | None = None, ansible: Path = ANSIBLE
) -> dict[str, list[str]]:
    """Each host's task files, by name under `tasks/`, in `HOSTS` order; empty hosts left out.

    `host` checks that host alone. It walks `main` when no play admits it, or when no playbook
    applies the role, because the caller named the host on purpose.

    Raises:
        core.UsageError: without `host`, no playbook applies the role, or a `when:` cannot be
            read for some host.
    """
    try:
        found = entries(role, ansible)
    except core.UsageError:
        if host is None:
            raise
        return {host: ["main"]}
    hosts = [host] if host else list(HOSTS)
    ended = role_end_hosts(role_dir, hosts)
    files: dict[str, list[str]] = {}
    for h in hosts:
        for entry in found:
            if entry.tasks_from == "main" and h in ended:
                continue
            if _admits(entry, h, forced=host is not None):
                if entry.tasks_from not in files.setdefault(h, []):
                    files[h].append(entry.tasks_from)
    files = {h: names for h, names in files.items() if names}
    return files or ({host: ["main"]} if host else {})
