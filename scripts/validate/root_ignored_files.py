#!/usr/bin/env python3
"""Flag a root-level file that `.gitignore`'s `/*` rule hides from git.

`.gitignore` opens with `/*`, so every root-level path is ignored unless a `!/<name>` rule
re-includes it. A new root file therefore stages nothing, commits nothing and lands nothing,
and CI and the merge both read green because neither can see a file git never tracked.
`LICENSE` and `SECURITY.md` were lost that way (PR #561), and `mkdocs.yml` and `.vale.ini`
hit the same trap. Until this check, only an agent remembering the rule prevented a third.

The check lists the untracked root entries git ignores, keeps the ones `git check-ignore -v`
attributes to the `/*` line itself, and drops the names in `LOCAL_ONLY`. Whatever is left is
either a file someone meant to commit, which needs a `!/<name>` line, or a new kind of local
clutter, which belongs in `LOCAL_ONLY`. A root entry ignored by a later, more specific rule
(`.pytest_cache/`, `/docs/superpowers/`) is deliberate and never flagged.

A tracked file is never "others", so nothing already in the tree can trip this. A clean CI
checkout holds no untracked files at all, so the check only ever fires in a working tree.

Run directly or via the ``validate-root-ignored-files`` prek hook. Exits non-zero when any
root entry is hidden by `/*` alone.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.repo_paths import REPO

# Root entries that are local by design. Each is a tool's cache or output, or a per-machine
# file nobody commits.
LOCAL_ONLY = frozenset(
    {
        ".venv",
        ".ruff_cache",
        ".ansible",
        "ansible.log",
        ".remember",
        ".mkdocs-strict-check",
        "site",
        # The fan-out harness's per-worktree output: the agent's brief, its report and its
        # stderr. Written by `scripts/dev/fanout_place.py` into each `.claude/worktrees/*`
        # checkout, so without this entry every fan-out agent's first commit fails this hook.
        ".fanout",
    }
)

DENY_ALL = "/*"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=60
    ).stdout


def ignored_root_entries(repo: Path) -> list[str]:
    """Untracked root-level entries git ignores, without a trailing slash."""
    out = _git(
        repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"
    )
    return sorted(
        {line.rstrip("/") for line in out.splitlines() if "/" not in line.rstrip("/")}
    )


def hidden_by_deny_all(repo: Path, names: list[str]) -> list[str]:
    """The subset of `names` whose deciding `.gitignore` rule is the `/*` line."""
    if not names:
        return []
    proc = subprocess.run(
        ["git", "check-ignore", "-v", "--no-index", "--", *names],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    hidden = []
    for line in proc.stdout.splitlines():
        source, _, path = line.partition("\t")
        if source.startswith(".gitignore:") and source.split(":", 2)[2] == DENY_ALL:
            hidden.append(path.rstrip("/"))
    return sorted(hidden)


def problems(repo: Path) -> list[str]:
    """Root entries hidden by `/*` that are neither re-included nor known to be local."""
    hidden = hidden_by_deny_all(repo, ignored_root_entries(repo))
    return [name for name in hidden if name not in LOCAL_ONLY]


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args(argv)
    found = problems(REPO)
    for name in found:
        print(
            f"{name}: hidden by .gitignore's `/*`, so git will never commit it. "
            f"To track it, add `!/{name}` to .gitignore. If it is local-only, add it to "
            f"LOCAL_ONLY in scripts/validate/root_ignored_files.py."
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
