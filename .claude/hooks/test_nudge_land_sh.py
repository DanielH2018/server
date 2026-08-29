#!/usr/bin/env python3
"""Tests for the land.sh nudge PreToolUse guard.

The guard denies two shapes: a command that blocks until CI finishes, and the third or later
CI-status read in one session. Everything else -- including the first two reads, `gh pr view`,
`gh pr merge` and land.sh itself -- must pass untouched.

Every rule is an accept/reject pair. A guard that fires on everything and one that fires on
nothing look identical from the passing side alone.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import io
import json
import os
import sys

_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nudge-land-sh.py")
sys.path.insert(0, os.path.dirname(_HOOK))
_spec = importlib.util.spec_from_file_location("nudge_land_sh", _HOOK)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# --- classify: blocking waits --------------------------------------------------------------


def test_gh_run_watch_is_a_watch():
    assert _mod.classify("gh run watch 12345") == "watch"


def test_gh_pr_checks_with_watch_flag_is_a_watch():
    assert _mod.classify("gh pr checks 620 --watch") == "watch"


def test_gh_pr_checks_without_watch_is_a_status_read():
    assert _mod.classify("gh pr checks 620") == "status"


# --- classify: what must pass --------------------------------------------------------------


def test_gh_pr_view_is_not_polling():
    assert _mod.classify("gh pr view 620 --json state") is None


def test_gh_pr_merge_is_not_polling():
    assert _mod.classify("gh pr merge 620 --squash") is None


def test_gh_api_is_not_polling():
    assert _mod.classify("gh api repos/o/r/commits/abc/status") is None


def test_unrelated_command_is_not_polling():
    assert _mod.classify("git log --oneline -5") is None


# --- classify: command shapes --------------------------------------------------------------


def test_a_later_pipeline_stage_is_still_matched():
    assert _mod.classify("git fetch && gh run watch 1") == "watch"


def test_a_flag_between_gh_and_the_subcommand_is_ignored():
    assert _mod.classify("gh --repo o/r run watch 1") == "watch"


def test_the_subcommand_words_must_be_adjacent():
    """Dropping flags would let a flag VALUE stand in for a subcommand."""
    assert _mod.classify("gh issue list --search run --label watch") is None


def test_unbalanced_quotes_are_declined_rather_than_guessed():
    assert _mod.classify("gh run watch 'oops") is None


def test_a_word_merely_containing_gh_is_not_matched():
    assert _mod.classify("highlight run watch") is None


# --- the session counter -------------------------------------------------------------------


def test_counter_increments_within_a_session(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    assert _mod.bump("sess-a") == 1
    assert _mod.bump("sess-a") == 2
    assert _mod.bump("sess-a") == 3


def test_counters_are_separate_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    _mod.bump("sess-a")
    _mod.bump("sess-a")
    assert _mod.bump("sess-b") == 1


def test_a_stale_counter_file_does_not_deny_a_fresh_session(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    _mod.bump("sess-a", now=0.0)
    assert _mod.bump("sess-a", now=_mod._COUNTER_TTL_S + 1) == 1


def test_a_session_id_with_path_characters_stays_inside_the_temp_dir(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    path = _mod._counter_path("../../etc/passwd")
    assert path.parent == tmp_path


# --- main: the decisions it emits ------------------------------------------------------------


def _run(monkeypatch, capsys, command, session="s", tmp_path=None):
    if tmp_path is not None:
        monkeypatch.setattr(_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    payload = {"session_id": session, "tool_input": {"command": command}}
    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main() == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def test_watch_is_denied_with_the_land_sh_form(monkeypatch, capsys, tmp_path):
    decision = _run(monkeypatch, capsys, "gh run watch 1", tmp_path=tmp_path)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "land.sh" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_first_two_status_reads_pass(monkeypatch, capsys, tmp_path):
    assert _run(monkeypatch, capsys, "gh pr checks 1", "s1", tmp_path) is None
    assert _run(monkeypatch, capsys, "gh pr checks 1", "s1", tmp_path) is None


def test_the_third_status_read_is_denied(monkeypatch, capsys, tmp_path):
    for _ in range(2):
        _run(monkeypatch, capsys, "gh pr checks 1", "s2", tmp_path)
    decision = _run(monkeypatch, capsys, "gh pr checks 1", "s2", tmp_path)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "land.sh" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_land_sh_itself_is_never_denied(monkeypatch, capsys, tmp_path):
    for _ in range(5):
        assert (
            _run(
                monkeypatch,
                capsys,
                "./scripts/deploy_tools/land.sh --pr 1 --since abc",
                "s3",
                tmp_path,
            )
            is None
        )


def test_an_unrelated_command_never_consumes_a_read(monkeypatch, capsys, tmp_path):
    for _ in range(5):
        _run(monkeypatch, capsys, "git status", "s4", tmp_path)
    assert _run(monkeypatch, capsys, "gh pr checks 1", "s4", tmp_path) is None


def test_empty_payload_is_ignored(monkeypatch, capsys):
    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO(""))
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""


def test_malformed_payload_is_ignored(monkeypatch, capsys):
    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO("{nope"))
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""
