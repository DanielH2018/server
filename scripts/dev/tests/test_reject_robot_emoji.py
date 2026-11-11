"""The commit-msg hook that rejects the robot emoji, and its wiring in prek.toml (#7978).

Run: uv run pytest scripts/dev/tests/test_reject_robot_emoji.py
"""

import tomllib

from dev.reject_robot_emoji import main
from lib.repo_paths import REPO

HARNESS_FOOTER = (
    "\N{ROBOT FACE} Generated with [Claude Code](https://claude.com/claude-code)"
)


def _main_on(tmp_path, message: str) -> int:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(message, encoding="utf-8")
    return main([str(path)])


def test_a_message_with_the_harness_footer_is_flagged(tmp_path, capsys):
    assert _main_on(tmp_path, f"Add a file\n\n{HARNESS_FOOTER}\n") == 1
    assert "line(s) 3" in capsys.readouterr().err


def test_a_message_with_the_plain_footer_is_clean(tmp_path):
    footer = "Generated with [Claude Code](https://claude.com/claude-code)"
    assert _main_on(tmp_path, f"Add a file\n\n{footer}\n") == 0


def test_the_emoji_in_a_git_comment_line_is_clean(tmp_path):
    """`git commit -v` and the editor template put `#` lines in the file; git strips them."""
    assert _main_on(tmp_path, f"Add a file\n\n# {HARNESS_FOOTER}\n") == 0


def test_prek_installs_and_runs_the_hook_at_the_commit_msg_stage():
    config = tomllib.loads((REPO / "prek.toml").read_text())
    assert "commit-msg" in config.get("default_install_hook_types", [])
    hooks = {
        hook["id"]: hook for repo in config["repos"] for hook in repo.get("hooks", [])
    }
    hook = hooks["reject-robot-emoji"]
    assert hook["stages"] == ["commit-msg"]
    assert "scripts/dev/reject_robot_emoji.py" in hook["entry"]
