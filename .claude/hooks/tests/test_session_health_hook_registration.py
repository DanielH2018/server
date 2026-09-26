#!/usr/bin/env python3
"""Tests for the banner's missing-hook-script arm (issue #2697).

`.claude/settings.json` names every hook by an absolute path into the PRIMARY checkout, so a
worktree cut from a fresher `origin/master` registers scripts that checkout does not have,
`/bin/sh` exits 127, and the tool call runs with the guard skipped. `fanout_lib/launch.py`
closed the fan-out half; this arm is how a hand-made worktree hears about it.

Every test drives `hooklib.hook_registration_lines` through its `checkout`, `read_settings` and
`exists` seams rather than patching a module attribute, because the monkeypatch ratchet
(`ansible/tests/_ratchet.py`) caps a new test module at zero patches on a first-party module.

Run: uv run pytest .claude/hooks/tests/test_session_health_hook_registration.py
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hooklib import hook_registration_lines as arm

# Derived from `~` because `_SETTINGS` registers `~/server/...` and the arm expands it: a
# literal `/home/ubuntu` fails on a CI runner whose HOME is `/home/runner`.
_ROOT = os.path.expanduser("~/server")
_CHECKOUT = f"{_ROOT}/.claude/worktrees/fanout-2697"
_HOOKS = f"{_ROOT}/.claude/hooks"

_SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": "~/server/.claude/hooks/old.sh"},
                    {"type": "command", "command": "~/server/.claude/hooks/new.sh"},
                ],
            }
        ]
    }
}


def _lines(settings=None, present=(f"{_HOOKS}/old.sh",)):
    """The banner lines, with only `present` existing on the primary checkout."""
    return arm.missing_hook_script_lines(
        checkout=_CHECKOUT,
        read_settings=lambda: _SETTINGS if settings is None else settings,
        exists=lambda path: path in set(present),
    )


def test_a_registered_script_the_primary_checkout_has_is_clean():
    assert _lines(present=(f"{_HOOKS}/old.sh", f"{_HOOKS}/new.sh")) == []


def test_a_registered_script_the_primary_checkout_lacks_is_flagged():
    (line,) = _lines()
    assert f"{_HOOKS}/new.sh" in line
    assert f"{_HOOKS}/old.sh" not in line, "a script that exists is not a finding"
    assert "127" in line, "the symptom the operator will have already seen"
    assert "SKIPPED" in line, "the consequence, not just the missing file"
    assert f"git -C {_ROOT} merge --ff-only origin/master" in line, (
        "the way out belongs in the line: the session reading it is in a worktree and "
        "cannot look at the primary checkout for itself"
    )
    assert _CHECKOUT not in line, (
        "the fix names the checkout holding the script, never the session's own worktree"
    )


def test_every_missing_script_is_named_once():
    settings = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"command": f"{_HOOKS}/a.sh"},
                        {"command": f"{_HOOKS}/b.sh"},
                    ]
                },
                {"hooks": [{"command": f"{_HOOKS}/a.sh"}]},
            ]
        }
    }
    (line,) = _lines(settings=settings, present=())
    assert line.count(f"{_HOOKS}/a.sh") == 1
    assert f"{_HOOKS}/b.sh" in line
    assert "2 hook script(s)" in line


def test_a_long_list_is_truncated_with_a_count():
    commands = [{"command": f"{_HOOKS}/h{n}.sh"} for n in range(arm.MISSING_LIMIT + 3)]
    settings = {"hooks": {"PreToolUse": [{"hooks": commands}]}}
    (line,) = _lines(settings=settings, present=())
    assert "+3 more" in line
    assert f"{_HOOKS}/h{arm.MISSING_LIMIT}.sh" not in line


@pytest.mark.parametrize(
    "error", [json.JSONDecodeError("bad", "{", 0), FileNotFoundError(_CHECKOUT)]
)
def test_a_settings_file_this_cannot_read_says_nothing(error):
    """Claude Code registered no hooks from it either, so nothing is missing."""

    def boom():
        raise error

    assert (
        arm.missing_hook_script_lines(
            checkout=_CHECKOUT, read_settings=boom, exists=lambda _p: False
        )
        == []
    )


def test_no_resolvable_checkout_says_nothing():
    assert arm.missing_hook_script_lines(checkout="", read_settings=dict) == []


def test_a_command_resolved_through_path_is_not_ruled_on():
    """`sh -c` style entries and bare binaries are not this arm's subject."""
    assert arm.script_path("prek run --all-files", _CHECKOUT) is None


def test_an_unexpanded_variable_is_not_ruled_on():
    """A path this cannot resolve must not read as a missing file."""
    assert arm.script_path("$SOME_UNSET_DIR/hooks/x.sh", _CHECKOUT) is None


def test_an_unparsable_command_is_not_ruled_on():
    assert arm.script_path('"/unbalanced/quote.sh', _CHECKOUT) is None


def test_claude_project_dir_resolves_to_the_sessions_own_checkout():
    """The documented variable, not the primary checkout the hook file itself lives in."""
    assert arm.script_path("$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh", _CHECKOUT) == (
        f"{_CHECKOUT}/.claude/hooks/x.sh"
    )


def test_a_relative_command_resolves_against_the_sessions_checkout():
    assert arm.script_path(".claude/hooks/x.sh", _CHECKOUT) == (
        f"{_CHECKOUT}/.claude/hooks/x.sh"
    )


def test_session_checkout_prefers_the_documented_variable():
    assert arm.session_checkout(env={"CLAUDE_PROJECT_DIR": _CHECKOUT}) == _CHECKOUT


def test_session_checkout_walks_up_to_the_settings_file_when_the_variable_is_unset(
    tmp_path,
):
    """Claude Code's variable is not relied on: no hook in this repo read it before."""
    root = tmp_path / "wt"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "settings.json").write_text("{}")
    deep = root / "ansible" / "roles"
    deep.mkdir(parents=True)
    assert arm.session_checkout(env={}, cwd=str(deep)) == str(root)


def test_session_checkout_answers_none_outside_any_checkout(tmp_path):
    """It never falls back to the hook file's own repo, which is the primary checkout."""
    assert arm.session_checkout(env={}, cwd=str(tmp_path)) is None


def test_the_cwd_the_caller_passes_decides_which_checkout_is_read(
    monkeypatch, tmp_path
):
    """The seam that makes or breaks the arm: a wrong cwd reads the wrong settings.json.

    `session-health.py` passes the SessionStart payload's `cwd`, because the hook's own process
    cwd is whatever Claude Code launched it with. If that resolved to the primary checkout
    instead, the arm would compare that checkout's settings against its own files, agree with
    itself, and return [] — indistinguishable from health for the case this exists to catch.
    """
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    root = tmp_path / "wt"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"hooks": [{"command": f"{_HOOKS}/absent.sh"}]}]
                }
            }
        )
    )
    (line,) = arm.missing_hook_script_lines(cwd=str(root / "ansible"))
    assert f"{_HOOKS}/absent.sh" in line


def test_the_repos_own_settings_registers_the_hooks_this_walk_must_find():
    """Non-vacuity: an empty walk is indistinguishable from a healthy checkout.

    `registered_hook_commands` finds its subjects by walking a JSON shape, so a settings schema
    change would return [] and leave both halves of every pair above green.
    """
    settings = json.loads(
        (Path(__file__).resolve().parents[1].parent / "settings.json").read_text()
    )
    commands = arm.registered_hook_commands(settings)
    assert len(commands) >= 9, commands
    names = {os.path.basename(c) for c in commands}
    assert {"session-health.sh", "bash-pretool.sh", "block-protected-edits.sh"} <= names
