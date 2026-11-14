#!/usr/bin/env python3
"""Generates docs/reference/size.md, the repo's size today and 30 and 90 days earlier.

The operator asked that every proposal name its net complexity delta (#8515). This page is
the baseline a proposal cites: tracked files and lines per top-level tree, test files,
instruction lines, and the count of rules, skills, agents and hooks. Each measure carries
its value at the last commit 30 and 90 days before the run, so growth between reviews shows
on the page. The page sets no budget and gates nothing.

A snapshot is one `git diff --numstat` from the empty tree to a commit, so a file's lines
are the lines git counts as added. Textconv is off: `secrets/secrets.yml` carries
`diff=sops`, and a textconv diff would decrypt it. Binary files count as files with no
lines.

Usage::

    uv run python scripts/docs/reference/size.py --out docs/reference/size.md
"""

import re
import sys as _sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.cli_help import ArgumentParser
from lib.docs_provenance import finish_generator, generated_banner
from lib.git import git, git_stdout
from lib.repo_paths import REPO

SOURCE = "scripts/docs/reference/size.py"
OFFSETS = (0, 30, 90)
ROOT_TREE = "(root files)"

_TEST_FILE = re.compile(r"(.*/)?test_[^/]*\.py")
_CLAUDE_MD = re.compile(r"(.*/)?CLAUDE\.md")
KINDS = (
    ("Rules", "`.claude/rules/*.md`", re.compile(r"\.claude/rules/[^/]+\.md")),
    (
        "Skills",
        "`.claude/skills/*/SKILL.md`",
        re.compile(r"\.claude/skills/[^/]+/SKILL\.md"),
    ),
    ("Agents", "`.claude/agents/*.md`", re.compile(r"\.claude/agents/[^/]+\.md")),
    ("Hooks", "`.claude/hooks/*.py`", re.compile(r"\.claude/hooks/[^/]+\.py")),
)


@dataclass(frozen=True)
class Snapshot:
    """The tracked files of one commit, with each file's line count."""

    days: int
    sha: str
    date: str
    lines: dict[str, int]

    def tree(self, name: str) -> tuple[int, int]:
        """Files and lines under the top-level tree `name`."""
        counts = [n for path, n in self.lines.items() if _tree_of(path) == name]
        return len(counts), sum(counts)

    def test_files(self) -> int:
        return sum(1 for path in self.lines if _TEST_FILE.fullmatch(path))

    def instruction_lines(self) -> int:
        return sum(
            n
            for path, n in self.lines.items()
            if path.startswith(".claude/") or _CLAUDE_MD.fullmatch(path)
        )

    def matching(self, pattern: re.Pattern) -> int:
        return sum(1 for path in self.lines if pattern.fullmatch(path))


def _tree_of(path: str) -> str:
    head, sep, _ = path.partition("/")
    return head if sep else ROOT_TREE


def _empty_tree(repo: _Path) -> str:
    return git_stdout("hash-object", "-t", "tree", "/dev/null", cwd=repo)


def _commit_before(repo: _Path, when: datetime) -> str | None:
    sha = git_stdout("rev-list", "-1", f"--before={when.isoformat()}", "HEAD", cwd=repo)
    return sha or None


def snapshot(repo: _Path, days: int, now: datetime) -> Snapshot | None:
    """The last commit at least `days` before `now`, or None when the history is shorter."""
    sha = _commit_before(repo, now - timedelta(days=days))
    if sha is None:
        return None
    out = git(
        "diff",
        "--numstat",
        "--no-renames",
        "--no-textconv",
        "--no-ext-diff",
        _empty_tree(repo),
        sha,
        cwd=repo,
    ).stdout
    lines = {}
    for row in out.splitlines():
        added, _deleted, path = row.split("\t", 2)
        lines[path] = int(added) if added.isdigit() else 0
    date = git_stdout("show", "-s", "--format=%cs", sha, cwd=repo)
    return Snapshot(days=days, sha=sha[:9], date=date, lines=lines)


def _cells(snaps: list[Snapshot | None], value) -> str:
    return " | ".join("—" if s is None else f"{value(s):,}" for s in snaps)


def _label(days: int) -> str:
    return "today" if days == 0 else f"{days} days earlier"


def _snapshot_line(snaps: list[Snapshot | None]) -> str:
    return "; ".join(
        f"*{_label(d)}* is `{s.sha}` ({s.date})"
        if s
        else f"*{_label(d)}* has no commit, so its cells read —"
        for s, d in zip(snaps, OFFSETS, strict=True)
    )


def render_markdown(snaps: list[Snapshot | None]) -> str:
    """Renders the size page: the provenance banner, the tree table and the measures table.

    Args:
        snaps: one snapshot per entry of `OFFSETS`, in that order; None where the history
            does not reach back that far.
    """
    columns = [_label(d) for d in OFFSETS]
    parts = [generated_banner(SOURCE), "# Repo size\n"]
    parts.append(
        "Tracked files and lines at HEAD and at the last commit 30 and 90 days before this "
        "page was generated. A proposal names its net complexity delta against these "
        "numbers. A line is one line of a tracked file as `git diff --numstat` counts it "
        "from the empty tree; a binary file counts as a file with no lines.\n"
    )
    parts.append(_snapshot_line(snaps) + ".\n")
    parts.append("## Files and lines per top-level tree\n")
    parts.append(
        "| Tree | " + " | ".join(f"Files {c} | Lines {c}" for c in columns) + " |"
    )
    parts.append("|---" + "|---:|---:" * len(columns) + "|")
    trees = {_tree_of(p) for s in snaps if s for p in s.lines}
    today = snaps[0]
    order = sorted(trees, key=lambda t: (-(today.tree(t)[1] if today else 0), t))
    for name in order:
        label = name if name == ROOT_TREE else f"`{name}/`"
        cells = " | ".join(
            "— | —" if s is None else "{:,} | {:,}".format(*s.tree(name)) for s in snaps
        )
        parts.append(f"| {label} | {cells} |")
    total = " | ".join(
        "— | —" if s is None else f"{len(s.lines):,} | {sum(s.lines.values()):,}"
        for s in snaps
    )
    parts.append(f"| **Total** | {total} |")
    parts.append("")
    parts.append("## Tests and instructions\n")
    parts.append(
        "A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines "
        "of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module "
        "directly under `.claude/hooks/`; its tests are not counted.\n"
    )
    parts.append("| Measure | " + " | ".join(columns) + " |")
    parts.append("|---" + "|---:" * len(columns) + "|")
    parts.append(f"| Test files | {_cells(snaps, Snapshot.test_files)} |")
    parts.append(
        "| Instruction lines (every `CLAUDE.md` plus `.claude/`) | "
        f"{_cells(snaps, Snapshot.instruction_lines)} |"
    )
    for label, glob, pattern in KINDS:
        parts.append(
            f"| {label} ({glob}) | {_cells(snaps, lambda s, p=pattern: s.matching(p))} |"
        )
    return "\n".join(parts) + "\n"


def main(
    argv: list[str] | None = None,
    repo: _Path = REPO,
    now: datetime | None = None,
) -> int:
    """Entry point: measures the repo at three points in its history and writes the page.

    Args:
        argv: command-line arguments, or None to use `sys.argv`.
        repo: the repository to measure; a test passes a scratch one.
        now: the clock the 30- and 90-day offsets count back from; UTC now when omitted.
    """
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=_Path, required=True, help="output file path")
    args = parser.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    snaps = [snapshot(repo, days, now) for days in OFFSETS]
    return finish_generator(
        "docs.reference.size", args.out, snaps, render_markdown, "snapshot"
    )


if __name__ == "__main__":
    raise SystemExit(main())
