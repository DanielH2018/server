#!/usr/bin/env python3
# gen-hooks: library
#   reason: an arm of bash-pretool.py, which run-hook.sh runs through `uv run python`
"""The `bash-pretool.py` arm that refuses a whole-suite pytest run inside a fan-out batch.

Usage: `bash-pretool.py` calls `decision(payload)` on every Bash call. Run standalone, it reads
the hook payload on stdin and prints the decision: `uv run --no-sync python
.claude/hooks/block-batch-full-suite.py < payload.json`.

WHY. A fan-out batch's local full-suite run was its largest memory spike. Measured on
daniel-box on 2026-10-18, the whole suite held 2.3 GiB of anon memory for about six minutes at
the four xdist workers a Claude session allows. The brief's guard set held 0.8 GiB for 52s.
The spike is what sizes each batch's 2.5 GiB reservation, and the reservation is what limits
how many batches a host fits. In the week to 2026-10-18, 89 of 222 batches ran the whole suite
locally, 143 times in all. CI runs the whole suite on every PR, and the review pipeline waits
on it and resumes the agent with the failing checks when it is red, so a local run repeats the
check CI already makes.

The operator chose CI as a batch's one full run on 2026-10-18 (#5667's follow-up). Of the 31
real failures local full runs caught that week, the guard set catches 17. The other 14, mostly
cross-role census tests far from the change, now arrive as a red CI run and its fix round.

THE RULE. Inside a fan-out batch's worktree, a pytest stage is denied when it names no test
path, or when the paths it names select more than `MAX_MODULES` test modules. `ansible/tests`
alone holds 537. Naming paths is the escape that matters, so a stage that names several
broad directories to get round the first arm meets the second. `--collect-only` and the
informational flags (`--help`, `--version`, `--markers`, `--fixtures`) are allowed.

A batch is recognised by its worktree: the payload's `cwd`, or `CLAUDE_PROJECT_DIR`, lies
inside `.claude/worktrees/fanout-<batch>` or `.claude/worktrees/claude+fanout-<batch>`. Every
other session keeps the whole suite.
"""

import os
import re
import sys
from pathlib import Path

from _hook_common import (
    Unsplittable,
    emit_pretooluse_decision,
    read_payload,
    split_stages,
    strip_shell_keywords,
)

# The brief's guard set selects 114 modules; `ansible/tests` alone selects 537 and the whole
# suite about 1,337.
MAX_MODULES = 300

_BATCH_TREE = re.compile(r"/\.claude/worktrees/(?:claude\+)?fanout-[^/]+(?:/|$)")

# Words that may stand before `pytest` in a stage: the launchers and their flags.
_LAUNCHERS = frozenset(
    {"uv", "run", "python", "python3", "-m", "timeout", "nice", "env", "stdbuf"}
)
# uv and launcher options whose value is the next word.
_LAUNCHER_VALUES = frozenset(
    {"--directory", "--project", "--python", "--with", "--group", "-k"}
)
# pytest options whose value is the next word, so that value is not read as a path.
_PYTEST_VALUES = frozenset(
    {
        "-k", "-m", "-n", "-p", "-c", "-o", "-W",
        "--deselect", "--ignore", "--ignore-glob", "--maxfail", "--tb", "--durations",
        "--rootdir", "--basetemp", "--junitxml", "--junit-xml", "--confcutdir",
        "--import-mode", "--log-level", "--dist", "--numprocesses", "--timeout",
    }
)  # fmt: skip
_INFO_FLAGS = frozenset(
    {
        "--collect-only",
        "--co",
        "--help",
        "-h",
        "--version",
        "-V",
        "--markers",
        "--fixtures",
    }
)

DENY_REASON = (
    "This is a fan-out batch, and a batch does not run the whole suite locally: CI runs it on "
    "the PR, and the review pipeline resumes you with each failing check if it goes red. A "
    "local full run held 2.3 GiB for about six minutes, which is what limits how many batches "
    "a host fits. Name the tests beside your change by path (`uv run pytest <test files or "
    "tests/ directory>`), then run the guard set the brief's 'Before you open the PR' step "
    "names."
)


def in_batch(cwd: str | None, project_dir: str | None) -> bool:
    """Whether the session runs inside a fan-out batch's worktree."""
    return any(_BATCH_TREE.search(p) for p in (cwd, project_dir) if p)


def pytest_args(words: list[str]) -> list[str] | None:
    """The arguments after `pytest` when this stage runs it, else None.

    Everything before `pytest` must be a launcher word, a flag, a launcher flag's value, an
    environment assignment or a timeout's duration, so `grep pytest notes.md` is not a run.
    """
    expect_value = False
    for i, word in enumerate(words):
        if expect_value:
            expect_value = False
            continue
        if word in ("pytest", "py.test"):
            return words[i + 1 :]
        if word in _LAUNCHER_VALUES:
            expect_value = True
        elif not (
            word in _LAUNCHERS
            or word.startswith("-")
            or "=" in word
            or re.fullmatch(r"\d+[smh]?", word)
        ):
            return None
    return None


def paths_named(args: list[str]) -> list[str]:
    """The positional arguments of a pytest command line: its test paths and node ids."""
    paths = []
    expect_value = False
    for word in args:
        if expect_value:
            expect_value = False
        elif word in _PYTEST_VALUES:
            expect_value = True
        elif word.startswith("-") or ">" in word or "<" in word or word == "&":
            continue
        else:
            paths.append(word)
    return paths


def modules_selected(paths: list[str], base: Path) -> int:
    """How many test modules `paths` select, counting a path it cannot see as one."""
    count = 0
    for arg in paths:
        path = Path(arg.split("::", 1)[0])
        if not path.is_absolute():
            path = base / path
        if path.is_dir():
            count += sum(1 for _ in path.rglob("test_*.py"))
        else:
            count += 1
    return count


def problem(command: str, cwd: Path) -> str | None:
    """The reason `command` is refused in a batch, or None."""
    try:
        stages = split_stages(command)
    except Unsplittable:
        return None
    for stage in stages:
        args = pytest_args(strip_shell_keywords(stage))
        if args is None or _INFO_FLAGS.intersection(args):
            continue
        paths = paths_named(args)
        if not paths:
            return DENY_REASON
        modules = modules_selected(paths, cwd)
        if modules > MAX_MODULES:
            return f"{DENY_REASON} This run selects {modules} test modules, over {MAX_MODULES}."
    return None


def decision(payload) -> tuple[str, str] | None:
    """The `(decision, reason)` pair for this payload, or None. The `bash-pretool.py` entry point."""
    command = (payload.get("tool_input") or {}).get("command", "")
    cwd = payload.get("cwd")
    if not command or not in_batch(cwd, os.environ.get("CLAUDE_PROJECT_DIR")):
        return None
    found = problem(command, Path(cwd or Path.cwd()))
    return ("deny", found) if found else None


def main() -> int:
    """Read the hook payload from stdin and emit the decision `decision` returns."""
    payload = read_payload()
    verdict = decision(payload) if payload else None
    if verdict:
        emit_pretooluse_decision(*verdict)
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main())
