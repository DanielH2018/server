#!/usr/bin/env python3
"""The lookups behind the rows `hooklib.instruction_rows` writes at SessionStart.

`test_session_health_instructions.py` covers the rows themselves through main(); these pin
the rules it does not reach, under a scratch HOME: the output-style lookup, and the primary
checkout's MEMORY.md for a linked worktree, which a plain clone cannot exercise.

Run: uv run pytest .claude/hooks
"""

import json
import os
import sys

from lib.git_testing import git, init_repo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _hook_common import INSTRUCTIONS_LOG_ENV
from hooklib import instruction_rows


def _home(tmp_path, monkeypatch, style):
    home = tmp_path / "home" / ".claude"
    (home / "output-styles").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"outputStyle": style}))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


def test_project_settings_name_the_style_over_the_users(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, "user-style")
    (home / "output-styles" / "user-style.md").write_text("u")
    (home / "output-styles" / "project-style.md").write_text("p")
    project = tmp_path / "repo"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"outputStyle": "project-style"})
    )
    assert instruction_rows.output_style_path(str(project)) == str(
        home / "output-styles" / "project-style.md"
    )


def test_a_style_whose_file_name_differs_is_found_by_its_frontmatter_name(
    tmp_path, monkeypatch
):
    home = _home(tmp_path, monkeypatch, "Terse Voice")
    (home / "output-styles" / "other.md").write_text("---\nname: other\n---\n")
    style = home / "output-styles" / "terse.md"
    style.write_text("---\nname: Terse Voice\ndescription: d\n---\nbody\n")
    assert instruction_rows.output_style_path(str(tmp_path / "repo")) == str(style)


def _slug(path):
    """Claude Code's project directory name, spelled out here rather than imported."""
    return "".join(c if c.isalnum() else "-" for c in str(path))


def test_a_linked_worktree_rows_its_primary_checkouts_memory_md(tmp_path, monkeypatch):
    # A scratch repo with a linked worktree, so the session's cwd and the primary checkout
    # differ wherever the suite runs, a plain CI clone included.
    primary = init_repo(tmp_path / "primary", initial_commit="i").resolve()
    worktree = tmp_path / "linked"
    git(primary, "worktree", "add", "-q", str(worktree))
    home = _home(tmp_path, monkeypatch, "builtin-style")
    primary_dir = home / "projects" / _slug(primary)
    worktree_dir = home / "projects" / _slug(worktree.resolve())
    for d, size in ((primary_dir, 3000), (worktree_dir, 1100)):
        (d / "memory").mkdir(parents=True)
        (d / "memory" / "MEMORY.md").write_bytes(b"m" * size)
    log = tmp_path / "instructions.log"
    monkeypatch.setenv(INSTRUCTIONS_LOG_ENV, str(log))

    instruction_rows.log_unevented_loads(
        {"session_id": "dddd4656", "cwd": str(worktree)}
    )

    rows = [r.split()[3:] for r in log.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        ["AutoMem", str(primary_dir / "memory" / "MEMORY.md"), "bytes=3000"]
    ]
