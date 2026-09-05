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

import ast
import os
import subprocess
from pathlib import Path
from typing import Iterator

from lib import yaml_fast
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
    return yaml_fast.safe_load(path.read_text())


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


# --- Python modules under a role's files/ ----------------------------------------------------
#
# A role's runtime modules can sit at any depth under files/ — `files/bridge/common.py` is the
# module `bridge.common` — and a guard that reads them must not assume one level. A one-level
# glob returns an EMPTY set the moment the modules move down a directory, and every `all(...)`
# over it passes; repo-root CLAUDE.md's "a check that finds its own subject by pattern" rule
# records five guards that broke exactly that way. These readers identify a module by its
# dotted path under the root, so a flat `bridge/config.py` and a nested `bridge/config.py`
# are each one id, and resolve an import to that id in every spelling Python allows.


def is_test_file(path: Path) -> bool:
    """Whether `path` is test code, by basename or by living under a `tests/` directory.

    Pass a path relative to the tree being judged (a repo-relative path, or one relative to
    the `root` a caller is walking). The directory clause reads every component, so an
    absolute path could match a `tests` component that is part of the checkout's own location
    rather than of the repo layout.
    """
    return (
        path.name.startswith("test_")
        or path.name == "conftest.py"
        or "tests" in path.parts[:-1]
    )


def module_id(path: Path, root: Path) -> str:
    """`root/bridge/common.py` -> "bridge.common"; `root/check.py` -> "check"."""
    return path.relative_to(root).with_suffix("").as_posix().replace("/", ".")


def python_modules(root: Path) -> dict[str, Path]:
    """Dotted id -> file for every runtime module under `root`, at any depth.

    Test files and `__pycache__` are excluded; a test suite belongs in the sibling `tests/`
    directory, and the name filter is the belt-and-braces check against one landing here.
    """
    return {
        module_id(p, root): p
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts and not is_test_file(p.relative_to(root))
    }


def imported_module_ids(tree: ast.AST, ids) -> set[str]:
    """The ids in `ids` that `tree` imports, in any form.

    `import a`, `import p.q [as r]`, `from p.q import X` and `from p import q` all count as an
    import of the module they name; a `from p.q import X` counts for `p.q`, not for `X`.
    """
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names if alias.name in ids}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module in ids:
                found.add(node.module)
            found |= {
                f"{node.module}.{alias.name}"
                for alias in node.names
                if f"{node.module}.{alias.name}" in ids
            }
    return found


def import_bindings(tree: ast.AST, ids) -> dict[str, str]:
    """Local name -> dotted prefix, for every import in `tree` that can reach one of `ids`.

    `import a` binds `a`; `import a as b` binds `b` to `a`; `import p.q` binds the package
    `p`, so `p.q.X` resolves through it; `import p.q as r` binds `r` to `p.q`; `from p import
    q [as r]` binds `q` (or `r`) to `p.q` when that is a module in `ids`. A `from p.q import X`
    binds a plain name, not a module, and is not recorded.
    """
    bound = {}
    packages = {i.rsplit(".", 1)[0] for i in ids if "." in i}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    if alias.name in ids:
                        bound[alias.asname] = alias.name
                    continue
                head = alias.name.split(".")[0]
                if (
                    head in ids
                    or head in packages
                    or any(p.startswith(head + ".") for p in packages)
                ):
                    bound[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if dotted in ids:
                    bound[alias.asname or alias.name] = dotted
    return bound


def module_of(node: ast.AST, bound: dict[str, str], ids) -> str | None:
    """The module id a `Name` or `a.b.c` attribute chain refers to, through `bound`, or None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in bound:
        return None
    dotted = ".".join([bound[node.id], *reversed(parts)])
    return dotted if dotted in ids else None


# The stub records one line per call so a test can assert it intercepted something. Without
# that record the fixture would be indistinguishable from one that silently stopped being on
# PATH: a stub no longer on PATH fails OPEN, the run stays green, and the real `logger` takes
# every call again.
_LOGGER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_LOGGER_CALLS"
"""


def stub_logger_on_path(tmp_path_factory, monkeypatch) -> Path:
    """Put a fake `logger` first on PATH so a test writes nothing to the host's syslog.

    Returns the file the stub appends to, one line per call, holding `logger`'s arguments
    verbatim (`-t <tag> <message>`).

    A test that runs a host script as a real subprocess inherits this when it builds its env
    from `os.environ`: its own stub directory, prepended later, still wins for `k3s` or `gh`,
    and this wins for `logger` over `/usr/bin/logger`. A script that starts calling `logger`
    by absolute path escapes it, which is why each directory using this also keeps one test
    asserting the stub RECORDED a call.

    Why it matters: the tags under test are shipped to Loki, so a fixture verdict written to
    the real syslog sits on a dashboard beside real ones. `scripts/deploy_tools/tests/conftest.py`
    measured 84% of the Landings board as fixtures before its copy of this existed, and issue
    #1052 found the backup-health reader's fixture verdicts in the Alert History board.
    """
    stub_dir = tmp_path_factory.mktemp("logger-stub")
    logger = stub_dir / "logger"
    logger.write_text(_LOGGER_STUB)
    logger.chmod(0o755)

    calls = stub_dir / "logger-calls"
    calls.touch()
    monkeypatch.setenv("TEST_LOGGER_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    return calls
