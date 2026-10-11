"""Every hook's first-party imports resolve when the hook runs the way Claude Code runs it.

An in-process test proves nothing about these imports: pytest's `pythonpath` puts `scripts/`
and `.claude/hooks` on `sys.path`, so any import resolves however stale the hook's own insert
is. A hook catches its own ImportError and fails open, so a broken insert does not show at
runtime either. session-health's insert went stale after the #443 regroup and emptied the
other-sessions banner for 7 days (#718). This file extends that subprocess check to every hook.

The hook set is derived: every `.py` under `.claude/hooks/` that imports anything outside the
stdlib, with the `run-hook.sh` mode of each `settings.json` entry that reaches it.

- `--no-project` runs under uv's bare interpreter with no venv. The probe runs under `-S`,
  which leaves out site-packages, from `/`.
- `--project` runs with the primary checkout as its cwd and the primary's venv, but the hook
  file comes from the session's checkout. The probe runs the scratch copy's hook from this
  checkout, with this checkout's venv, and every first-party module it loads must come from
  the scratch copy. A venv `.pth` naming this checkout would make the imports succeed against
  the wrong tree, which only this assertion sees.

`_hook_import_probe.py` documents how each import's own `sys.path` inserts are applied.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib.repo_paths import REPO

HOOKS = REPO / ".claude" / "hooks"
PROBE = Path(__file__).with_name("_hook_import_probe.py")
SKIPPED_DIRS = frozenset({"tests", "__pycache__"})
OUT_OF_TREE = frozenset({"claude_guard", "_claude_guard"})
# The top-level trees a hook's imports reach: the hooks themselves, `scripts/`, and the role
# `files/` directories `lib.repo_paths` names.
COPIED = (".claude/hooks", "scripts", "ansible")
# Anchored: the fallback after `||` names `run-hook.sh` again in its message text.
_ENTRY = re.compile(
    r'"\$CLAUDE_PROJECT_DIR"/\.claude/hooks/run-hook\.sh ([\w.-]+)((?: --\S+)*)'
)

# Members the census must find, with the mode each one runs under. Every `--no-project`
# entry is here, plus the `--project` guards that import from `scripts/`.
MUST_FIND = {
    "session-health.py": {"no-project"},
    "inject-nested-docs.py": {"project"},
    "log-instructions.py": {"no-project"},
    "fanout-stop.py": {"no-project"},
    "bash-pretool.py": {"project"},
    "block-protected-bash.py": {"project"},
    "block-footguns.py": {"project"},
    "_facts_line.py": {"project"},
    "block-protected-edits.py": {"project"},
    "auto-mode-bridge.py": {"project"},
}


def _hook_files() -> dict[str, Path]:
    return {
        path.relative_to(HOOKS).as_posix(): path
        for path in sorted(HOOKS.rglob("*.py"))
        if not SKIPPED_DIRS & set(path.relative_to(HOOKS).parts)
    }


def _entries() -> dict[str, set[str]]:
    """The hook each `settings.json` command runs, and the mode it runs that hook in."""
    entries: dict[str, set[str]] = {}
    for groups in json.loads((REPO / ".claude/settings.json").read_text())[
        "hooks"
    ].values():
        for group in groups:
            for hook in group["hooks"]:
                match = _ENTRY.match(hook["command"])
                if match:
                    name, flags = match.groups()
                    mode = "project" if flags.strip() else "no-project"
                    entries.setdefault(f"{name}.py", set()).add(mode)
    return entries


def _imported_names(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _siblings(path: Path, files: dict[str, Path]) -> set[str]:
    """The hook files `path` imports by name or loads by path, as keys of `files`."""
    found = set()
    for name in _imported_names(path):
        dotted = name.replace(".", "/")
        found |= {
            key for key in (f"{dotted}.py", f"{dotted}/__init__.py") if key in files
        }
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            stem = node.value.removesuffix(".py")
            if f"{stem}.py" in files:
                found.add(f"{stem}.py")
    return found


def census() -> dict[str, set[str]]:
    """Every hook file that imports beyond the stdlib, with the modes that reach it."""
    files = _hook_files()
    modes: dict[str, set[str]] = {}
    for entry, entry_modes in _entries().items():
        seen, todo = set(), [entry]
        while todo:
            key = todo.pop()
            if key in seen or key not in files:
                continue
            seen.add(key)
            todo += _siblings(files[key], files)
        for key in seen:
            modes.setdefault(key, set()).update(entry_modes)
    return {
        key: found
        for key, found in modes.items()
        if any(
            name.partition(".")[0] not in sys.stdlib_module_names | OUT_OF_TREE
            for name in _imported_names(files[key])
        )
    }


CENSUS = census()


@pytest.fixture(scope="module")
def scratch(tmp_path_factory) -> Path:
    """A copy of the checkout holding every module a hook can import, and no venv."""
    root = tmp_path_factory.mktemp("scratch-checkout")
    for top in COPIED:
        shutil.copytree(
            REPO / top,
            root / top,
            ignore=lambda d, names: [
                n
                for n in names
                if n in SKIPPED_DIRS
                or (not n.endswith(".py") and not (Path(d) / n).is_dir())
            ],
        )
    return root


def probe(scratch: Path, hook: str, mode: str, env_extra: dict | None = None) -> dict:
    env = {
        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "VIRTUAL_ENV"}
    }
    env.update(env_extra or {})
    flags = ["-S"] if mode == "no-project" else []
    proc = subprocess.run(
        [sys.executable, *flags, str(PROBE), str(scratch / ".claude/hooks" / hook),
         str(scratch), *(str(REPO / top) for top in COPIED)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd="/" if mode == "no-project" else REPO,
        check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def problems(result: dict, scratch: Path) -> list[str]:
    """Each import that failed, and each first-party module loaded from outside `scratch`."""
    wrong_tree = [
        f"{name} loaded from {where}, not the hook's own checkout"
        for name, where in sorted(result["files"].items())
        if not Path(os.path.realpath(where)).is_relative_to(os.path.realpath(scratch))
    ]
    return result["failures"] + wrong_tree


def test_the_census_finds_each_named_hook_in_its_mode():
    missing = {
        hook: sorted(modes - CENSUS.get(hook, set()))
        for hook, modes in MUST_FIND.items()
        if not modes <= CENSUS.get(hook, set())
    }
    assert not missing, f"the census lost these hooks or modes: {missing}"


@pytest.mark.parametrize(
    ("hook", "mode"),
    sorted((hook, mode) for hook, modes in CENSUS.items() for mode in modes),
)
def test_a_hook_imports_its_first_party_modules_from_its_own_checkout(
    scratch, hook, mode
):
    found = problems(probe(scratch, hook, mode), scratch)
    assert not found, (
        f"{hook} under run-hook.sh's --{mode} mode cannot import what it needs from its own "
        "checkout. A hook fails open on this, so nothing else reports it:\n"
        + "\n".join(found)
    )


def _stale_session_health(scratch: Path) -> str:
    """A copy of session-health whose `scripts/` inserts point one directory off, as in #718."""
    text = (scratch / ".claude/hooks/session-health.py").read_text()
    stale = 'os.path.join(REPO, "scripts")'
    assert text.count(stale) >= 2, "session-health no longer inserts scripts/ this way"
    name = "stale-session-health.py"
    (scratch / ".claude/hooks" / name).write_text(
        text.replace(stale, 'os.path.join(REPO, "scripts", "dev")')
    )
    return name


def test_a_stale_insert_is_reported(scratch):
    hook = _stale_session_health(scratch)
    try:
        found = problems(probe(scratch, hook, "no-project"), scratch)
    finally:
        (scratch / ".claude/hooks" / hook).unlink()
    assert any(f.startswith("lib.worktrees: ") for f in found), found


def test_a_module_from_another_checkout_is_reported(scratch):
    """The stale insert imports cleanly when this checkout's `scripts/` is on the path."""
    hook = _stale_session_health(scratch)
    try:
        result = probe(scratch, hook, "project", {"PYTHONPATH": str(REPO / "scripts")})
    finally:
        (scratch / ".claude/hooks" / hook).unlink()
    assert not result["failures"]
    assert any(
        p.startswith("lib.worktrees loaded from ") for p in problems(result, scratch)
    )


# A review pipeline runs every phase after the implementer with each hook pointed at a held
# copy outside the worktree (`fanout_lib.review.held_hooks`). A hook that finds `scripts/`
# through its own `__file__` finds the held copy's, so the copy must carry it (#4420).
@pytest.fixture(scope="module")
def held(tmp_path_factory) -> Path:
    from fanout_lib.review.held_hooks import read_hooks, write_hooks

    root = tmp_path_factory.mktemp("held")
    write_hooks(read_hooks(REPO), root)
    return root


@pytest.mark.parametrize(
    ("hook", "mode"),
    sorted(
        (hook, mode)
        for hook, modes in CENSUS.items()
        if hook != "session-health.py"
        for mode in modes
    ),
)
def test_a_held_hook_imports_its_first_party_modules_from_the_held_copy(
    held, hook, mode
):
    found = problems(probe(held, hook, mode), held)
    assert not found, f"{hook} cannot import from the held copy:\n" + "\n".join(found)


def _held_footguns(root: Path, expr: str) -> str:
    """`expr`, evaluated over block-footguns' globals as the copy at `root` loads them."""
    hooks = root / ".claude/hooks"
    code = (
        f"import runpy, sys; sys.path[0] = {str(hooks)!r}; "
        f"print(eval({expr!r}, runpy.run_path({str(hooks / 'block-footguns.py')!r})))"
    )
    env = {
        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "VIRTUAL_ENV"}
    }
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=REPO,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout.strip()


def test_the_held_ssh_guard_reads_the_inventory_hosts(held):
    assert "daniel-box" in _held_footguns(held, "_ssh_hosts()")


@pytest.fixture
def hooks_alone(tmp_path) -> Path:
    """A copy of the hooks with no `scripts/` or `ansible/` beside it."""
    shutil.copytree(
        REPO / ".claude/hooks",
        tmp_path / ".claude/hooks",
        ignore=shutil.ignore_patterns("tests"),
    )
    return tmp_path


def test_a_held_copy_of_the_hooks_alone_cannot_read_the_inventory(hooks_alone):
    assert _held_footguns(hooks_alone, "_ssh_hosts()") == "None"


# With no inventory the guard cannot tell a homelab host from any other, so it matches a
# remote git on every host rather than on none (#4420). A cd into the repo still passes.
def test_the_remote_git_rule_fails_closed_when_the_inventory_is_unreadable(
    hooks_alone,
):
    denied = _held_footguns(
        hooks_alone, "remote_git_problem(['ssh', 'daniel-box', 'git status'])"
    )
    assert "could not read the inventory" in denied
    allowed = _held_footguns(
        hooks_alone,
        "remote_git_problem(['ssh', 'daniel-box', 'cd /home/ubuntu/server; git status'])",
    )
    assert allowed == "None"
