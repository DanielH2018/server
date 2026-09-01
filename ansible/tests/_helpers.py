"""Paths and task-file readers shared by the guards in this directory.

Every guard here reads the repo's own Ansible sources, so they all re-derived the same handful
of roots and the same YAML walk. That cost more than the duplication: `_flatten` existed in two
incompatible shapes under one name, and which one a file got depended on which sibling it was
copied from. Both shapes are real and both are needed — they are `walk_tasks` and `leaf_tasks`
below, and the choice is now something a reader makes rather than inherits.

Imported by name (`from _helpers import ...`) rather than through a conftest fixture: these are
plain functions over the filesystem, and the modules here already reach `_k8s_render` the same
way.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator

import yaml
from ansible.plugins.filter.core import FilterModule
from ansible.plugins.filter.mathstuff import FilterModule as _MathFilters
from ansible.plugins.test.core import TestModule as _AnsibleTests
from jinja2 import FileSystemLoader
from jinja2.nativetypes import NativeEnvironment

REPO = Path(__file__).resolve().parents[2]
ANSIBLE = REPO / "ansible"
ROLES = ANSIBLE / "roles"
K8S_ROLES = ROLES / "k8s"
SETUP_ROLES = ROLES / "setup"
CONTAINER_ROLES = ROLES / "containers"
INVENTORY = ANSIBLE / "inventory"
HOST_VARS = INVENTORY / "host_vars"
GROUP_VARS = INVENTORY / "group_vars"
ALL_VARS = GROUP_VARS / "all.yml"
DEPLOY_PLAYBOOK = ANSIBLE / "deploy.yml"

_MODULE_KEYS = ("ansible.builtin.command", "ansible.builtin.shell")
_NESTING_KEYS = ("block", "rescue", "always")


def load_yaml(path: Path):
    """The parsed document, or None for an empty file."""
    return yaml.safe_load(path.read_text())


def load_tasks(path: Path) -> list[dict]:
    """The task list in a tasks file, empty for a file with no tasks."""
    return load_yaml(path) or []


def walk_tasks(tasks) -> Iterator[dict]:
    """Every task, including the `block:` wrappers themselves.

    Use this when the assertion is about a property some task has — a `become:`, a module, a
    registered name. The wrapper carries `when:` and `tags:` for its children, so a check that
    skipped it would miss the conditions that actually govern them.
    """
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in _NESTING_KEYS:
            yield from walk_tasks(task.get(key))


def leaf_tasks(tasks) -> list[dict]:
    """Only the tasks that run something, in run order, with wrappers dropped.

    Use this when the assertion is about ORDER — "the apply comes before the wait". A wrapper
    occupies an index without running anything, so including it shifts every position after it
    and an off-by-one reads as a real ordering violation.
    """
    out: list[dict] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        nested = [task[key] for key in _NESTING_KEYS if task.get(key)]
        if nested:
            for section in nested:
                out += leaf_tasks(section)
        else:
            out.append(task)
    return out


def command_of(task: dict) -> str:
    """The command a task runs, for either module shape, or "" if it runs neither.

    `command`/`shell` both accept a bare string or a dict with `cmd:`, and a guard that handles
    only one shape silently matches nothing against the other.
    """
    for key in _MODULE_KEYS:
        module = task.get(key)
        if isinstance(module, dict):
            return str(module.get("cmd", ""))
        if isinstance(module, str):
            return module
    return ""


def task_named(tasks, fragment: str) -> dict:
    """The one task whose name contains `fragment`.

    Raises if it matches none or several, rather than returning the first — a guard built on a
    name that has since been split across two tasks would otherwise keep asserting against
    whichever one happened to sort first.
    """
    matches = [t for t in walk_tasks(tasks) if fragment in str(t.get("name", ""))]
    assert len(matches) == 1, (
        f"{fragment!r} matched {len(matches)} tasks, expected exactly 1"
    )
    return matches[0]


def jinja_env() -> NativeEnvironment:
    """A NativeEnvironment carrying Ansible's own filters and tests.

    `NativeEnvironment` returns real Python objects where a plain Jinja2 environment would hand
    back a string repr — needed by any expression under test that produces a structure rather
    than text. The filters and tests come from Ansible's own plugin modules so an expression
    renders against the same code Ansible runs, not a reimplementation of it.
    """
    # The loader is what lets a template under test `{% import %}` a shared macro from
    # ansible/templates/. Without one, Jinja raises `TypeError: no loader for this environment
    # specified` at render time — an error naming the environment rather than the macro, which
    # reads as a broken helper rather than a template that reaches outside itself.
    env = NativeEnvironment(loader=FileSystemLoader(str(ANSIBLE / "templates")))
    env.filters.update(FilterModule().filters())
    # `difference`, `union`, `intersect` and the rest of the set filters live in mathstuff, NOT
    # core — an expression using one renders as `TemplateAssertionError: No filter named
    # 'difference'` without this, which reads as a broken test rather than a missing plugin
    # module. Found while writing the claude-otel dashboard-prune guard, whose three prunes are
    # all `difference()`.
    env.filters.update(_MathFilters().filters())
    env.tests.update(_AnsibleTests().tests())
    return env


def render_expr(expression: str, **context):
    """Render a Jinja expression through `jinja_env()`."""
    return jinja_env().from_string(expression).render(**context)


_DOC_EXCLUDED_DIRS = (
    REPO / "docs" / "archive",
    REPO / "ansible" / "roles" / "containers" / "archive",
)


def discover_docs() -> list[Path]:
    """Every operator doc in THIS checkout: repo CLAUDE.md files, `.claude/`, and `docs/`.

    DECIDED: `git ls-files`, not `rglob`. An rglob walks whatever is on disk, and this repo
    grows other checkouts during ordinary work — `.claude/worktrees/<name>/` is a full working
    tree per live session. Those hold OLDER copies of these same docs, so a guard that walks
    them judges this commit against other sessions' history and fails on paths that moved
    perfectly legitimately.

    That is not hypothetical: it broke the `docs-refresh` cron. Its commit runs the prek hooks,
    both doc guards failed on citations inside sibling worktrees, and the script took its
    designed failure path — "commit failed (hook rejection?); unstaged, nothing published" —
    so the generated reference pages silently stopped publishing. Master CI stayed green
    throughout, because a CI runner has no worktrees on disk. Found 2026-08-27.

    `git ls-files` answers with what this commit actually contains, which is the only thing
    these guards have any business asserting about.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    docs = []
    for rel in listed.split("\0"):
        if not rel:
            continue
        path = REPO / rel
        if path.name != "CLAUDE.md" and rel.split("/")[0] not in (".claude", "docs"):
            continue
        if any(excluded in path.parents for excluded in _DOC_EXCLUDED_DIRS):
            continue
        docs.append(path)
    return sorted(set(docs))
