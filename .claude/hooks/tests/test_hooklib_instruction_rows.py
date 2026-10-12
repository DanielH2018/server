#!/usr/bin/env python3
"""The output-style lookup `hooklib.instruction_rows` rows at SessionStart.

`test_session_health_instructions.py` covers the rows themselves through main(); these pin
the two lookup rules it does not reach, under a scratch HOME.

Run: uv run pytest .claude/hooks
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
