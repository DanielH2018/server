"""Every entry at the repo root is tracked by git or in the set the root ignore expects.

`.gitignore` opens with `/*` and allow-lists each tracked root entry by name, so a new
root-level file is invisible to `git status` and never reaches a commit. That is the
mechanism that once left a tool's output directory untracked at the root until it parked the
deployer (repo CLAUDE.md, *Pre-commit Hooks*: "one untracked file parks the deployer"). The
inverse also hides: a tool that writes a root file nobody expects, e.g. a `.pytest_cache` or
a `site/` from a bare `mkdocs build`, sits there ignored and unexplained.

Run: uv run pytest ansible/tests/repo/test_root_entries_are_tracked_or_expected.py
"""

import subprocess

from _helpers import REPO

# Root entries the ignore rule is expected to hide. Each is a tool's own working state.
EXPECTED_IGNORED = frozenset(
    {
        ".git",
        ".venv",  # uv's environment
        ".fanout",  # the issue-fanout bus (scripts/dev/fanout_*.py)
        ".remember",  # the remember plugin's session store
        ".pytest_cache",
        ".ruff_cache",
        ".ansible",  # ansible's own tmp/collections cache when it runs from the root
        "ansible.log",  # `log_path = ./ansible.log` in ansible.cfg
        "site",  # a bare `mkdocs build` (the cron builds to a temp dir)
        # the prek `mkdocs-strict` hook builds here and leaves it, on purpose (prek.toml)
        ".mkdocs-strict-check",
        ".mypy_cache",
        ".superpowers",  # SDD task specification directories
    }
)


def _tracked_root_entries() -> set[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return {rel.split("/", 1)[0] for rel in listed.split("\0") if rel}


def unexpected(root_entries: set[str], tracked: set[str]) -> list[str]:
    return sorted(root_entries - tracked - EXPECTED_IGNORED)


def test_every_root_entry_is_tracked_or_expected():
    tracked = _tracked_root_entries()
    assert {"ansible", "scripts", "pyproject.toml"} <= tracked
    on_disk = {p.name for p in REPO.iterdir()}
    assert unexpected(on_disk, tracked) == [], (
        "root entries git neither tracks nor this guard expects — add the file to "
        "`.gitignore`'s allow-list and commit it, or name it in EXPECTED_IGNORED with a reason"
    )


def test_a_stray_root_entry_is_flagged():
    assert unexpected({"ansible", ".venv", "stray.txt"}, {"ansible"}) == ["stray.txt"]
    assert unexpected({"ansible", ".venv"}, {"ansible"}) == []
