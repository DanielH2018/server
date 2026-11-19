"""What a branch adds to the tree: one count line, then each new name that nothing calls.

`repo_guards.py` prints this as advice before a PR opens, so a session reads how much its
change grew the tree while it can still shrink it. Every count is net of what the branch
removes, so a move or a replacement counts as nothing.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from lib.git import git
from lib.path_kinds import is_doc, is_test_only_path

_FLAG = re.compile(r"""add_argument\(\s*["']--""")
_TEST = re.compile(r"\s*(?:async\s+)?def test_")


def _is_module(path: str) -> bool:
    return path.endswith(".py") and not is_test_only_path(path)


def _changes(repo: Path, base: str) -> list[tuple[str, str, str]]:
    """Each changed path as (status letter, its path at ``base``, its path in the tree)."""
    out = git("diff", "--no-textconv", "--name-status", "-M", base, cwd=repo).stdout
    rows = (line.split("\t") for line in out.splitlines())
    return [(status[0], paths[0], paths[-1]) for status, *paths in rows]


def _net_lines(repo: Path, base: str) -> tuple[int, int]:
    """The net lines the branch adds outside tests and docs, and the net lines in tests."""
    code = tests = 0
    out = git(
        "diff", "--no-textconv", "--numstat", "--no-renames", base, cwd=repo
    ).stdout
    for added, deleted, path in (line.split("\t") for line in out.splitlines()):
        if added == "-" or is_doc(path):
            continue
        net = int(added) - int(deleted)
        if is_test_only_path(path):
            tests += net
        else:
            code += net
    return code, tests


def _changed_lines(repo: Path, base: str) -> Iterator[tuple[str, int, str]]:
    """Each Python line the branch adds or removes, as (path, +1 or -1, text)."""
    path = ""
    out = git("diff", "-M", "-U0", base, "--", "*.py", cwd=repo).stdout
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = line[6:]
        elif line.startswith(("+", "-")) and not line.startswith("--- "):
            yield path, 1 if line[0] == "+" else -1, line[1:]


def _top_level(source: str) -> set[str]:
    """The functions and classes a module defines at its top level."""
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return set()
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return {node.name for node in body if isinstance(node, kinds)}


def _uncalled(repo: Path, base: str, changes: list[tuple[str, str, str]]) -> list[str]:
    """Each top-level name the branch adds that no file outside tests and docs uses.

    A use is the name as a whole word in a tracked file, so a caller in YAML, a shell script
    or a hook table counts. The definition is itself one use.
    """
    found = []
    for status, before, path in changes:
        if status == "D" or not _is_module(path):
            continue
        old = "" if status == "A" else git("show", f"{base}:{before}", cwd=repo).stdout
        added = _top_level((repo / path).read_text()) - _top_level(old) - {"main"}
        for name in sorted(added):
            hits = git("grep", "-c", "-w", "-F", "-e", name, cwd=repo, check=False)
            counts = (line.rpartition(":") for line in hits.stdout.splitlines())
            uses = sum(
                int(count)
                for file, _, count in counts
                if not is_test_only_path(file) and not is_doc(file)
            )
            if uses <= 1:
                found.append(f"{path}:{name}")
    return found


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if abs(number) == 1 else f"{number} {noun}s"


def added_advice(repo: Path, since: str) -> str | None:
    """The count line for what the branch adds, then the new names with no caller.

    Empty when the branch adds nothing, and None when ``since`` and HEAD share no merge base.
    """
    merge_base = git("merge-base", since, "HEAD", cwd=repo, check=False)
    if merge_base.returncode != 0:
        return None
    base = merge_base.stdout.strip()
    changes = _changes(repo, base)
    modules = sum(
        {"A": 1, "D": -1}.get(status, 0)
        for status, _, path in changes
        if _is_module(path)
    )
    flags = tests = 0
    for path, sign, text in _changed_lines(repo, base):
        if is_test_only_path(path):
            tests += sign * bool(_TEST.match(text))
        else:
            flags += sign * bool(_FLAG.search(text))
    code, test_lines = _net_lines(repo, base)
    uncalled = _uncalled(repo, base, changes)
    if max(modules, flags, tests, code, test_lines) <= 0 and not uncalled:
        return ""
    lines = [
        f"branch adds: {_count(modules, 'module')}, {_count(flags, 'flag')}, "
        f"{_count(tests, 'test')}, {_count(code, 'line')} outside tests and docs, "
        f"{_count(test_lines, 'line')} of tests. Each count is net of what the branch removes."
    ]
    if uncalled:
        lines.append(
            "  no caller outside tests. Delete each, or say in the PR body what calls it: "
            + ", ".join(uncalled)
        )
    return "\n".join(lines)
