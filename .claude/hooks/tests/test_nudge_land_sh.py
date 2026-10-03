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
import tempfile


_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nudge-land-sh.py"
)
sys.path.insert(0, os.path.dirname(_HOOK))
_spec = importlib.util.spec_from_file_location("nudge_land_sh", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from _hook_common import Unsplittable  # noqa: E402


# --- classify: blocking waits --------------------------------------------------------------


def test_gh_run_watch_is_a_watch():
    assert _mod.classify("gh run watch 12345") == "watch"


def test_gh_pr_checks_with_watch_flag_is_a_watch():
    assert _mod.classify("gh pr checks 620 --watch") == "watch"


def test_gh_pr_checks_web_flag_is_a_status_read():
    """`-w` is `--web`, not a short `--watch`: it opens a browser and returns."""
    assert _mod.classify("gh pr checks 1 -w") == "status"


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


def test_a_semicolon_joined_later_stage_is_still_matched():
    """`;` joined the same way as `&&` above must still be caught — `shlex.split`
    glues an unquoted `;` onto the word before it, so `_hook_common.split_stages` must cut on
    `;` first for the `gh run watch` after it to be its own stage."""
    assert _mod.classify("git fetch; gh run watch 1") == "watch"


def test_a_flag_between_gh_and_the_subcommand_is_ignored():
    assert _mod.classify("gh --repo DanielH2018/server run watch 1") == "watch"


def test_the_subcommand_words_must_be_adjacent():
    """Dropping flags would let a flag VALUE stand in for a subcommand."""
    assert _mod.classify("gh issue list --search run --label watch") is None


def test_unbalanced_quotes_are_declined_rather_than_guessed():
    """A refused parse is no nudge, not an `ask`: bash refuses the same text, and a missed
    nudge costs one hand-written poll."""
    assert _mod.classify("gh run watch 'oops") is None


def test_a_missing_segmenter_is_declined_rather_than_asked():
    """The half-deployed host. An `ask` on every command for the sake of a nudge is a hook the
    operator turns off; the deny guards with a real cost ask instead."""

    def missing(command):
        raise Unsplittable("segmenter-missing", "not deployed")

    assert _mod.classify("gh run watch", split=missing) is None


def test_a_word_merely_containing_gh_is_not_matched():
    assert _mod.classify("highlight run watch") is None


# --- classify: repository scope ---------------------------------------------------


def test_another_repos_checks_are_out_of_scope():
    """land.sh lands only this repo's PRs, so its advice is wrong for the dotfiles repo."""
    assert _mod.classify("gh pr checks 5 --repo DanielH2018/dotfiles") is None
    assert _mod.classify("gh pr checks 5 --watch -R DanielH2018/dotfiles") is None


def test_this_repo_named_explicitly_is_still_in_scope():
    assert _mod.classify("gh pr checks 5 --repo DanielH2018/server --watch") == "watch"
    assert _mod.classify("gh pr checks 5 --repo=danielh2018/Server") == "status"


def test_the_attached_and_host_prefixed_repo_forms_are_read():
    assert _mod.classify("gh run list -RDanielH2018/dotfiles") is None
    assert _mod.classify("gh run list --repo=github.com/DanielH2018/dotfiles") is None
    assert _mod.classify("gh run list --repo github.com/DanielH2018/server") == "status"


def test_a_github_url_names_the_repo_too():
    assert (
        _mod.classify("gh pr checks https://github.com/DanielH2018/dotfiles/pull/5")
        is None
    )
    assert (
        _mod.classify("gh pr checks https://github.com/DanielH2018/server/pull/5")
        == "status"
    )


def test_another_repo_in_one_stage_does_not_hide_this_repos_poll_in_the_next():
    command = "gh pr checks 5 -R DanielH2018/dotfiles; gh run watch 1"
    assert _mod.classify(command) == "watch"


def test_a_failed_runs_log_is_a_one_shot_read_not_a_poll():
    assert _mod.classify("gh run view 123 --log-failed") is None
    assert _mod.classify("gh run view 123 --log") is None
    assert _mod.classify("gh run view 123") == "status"


# --- the session counter -------------------------------------------------------------------


def test_counter_increments_within_a_session(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    assert _mod.bump("sess-a") == 1
    assert _mod.bump("sess-a") == 2
    assert _mod.bump("sess-a") == 3


def test_counters_are_separate_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    _mod.bump("sess-a")
    _mod.bump("sess-a")
    assert _mod.bump("sess-b") == 1


def test_a_stale_counter_file_does_not_deny_a_fresh_session(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    _mod.bump("sess-a", now=0.0)
    assert _mod.bump("sess-a", now=_mod._COUNTER_TTL_S + 1) == 1


# --- main: the decisions it emits ------------------------------------------------------------


def _run(monkeypatch, capsys, command, session="s", tmp_path=None):
    if tmp_path is not None:
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
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


def test_land_py_itself_is_never_denied(monkeypatch, capsys, tmp_path):
    for _ in range(5):
        assert (
            _run(
                monkeypatch,
                capsys,
                "uv run python scripts/deploy_tools/land.py --pr 1 --since abc > /tmp/l.log 2>&1",
                "s6",
                tmp_path,
            )
            is None
        )


def test_an_unrelated_command_never_consumes_a_read(monkeypatch, capsys, tmp_path):
    for _ in range(5):
        _run(monkeypatch, capsys, "git status", "s4", tmp_path)
    assert _run(monkeypatch, capsys, "gh pr checks 1", "s4", tmp_path) is None


def test_another_repos_reads_never_consume_a_read(monkeypatch, capsys, tmp_path):
    for _ in range(5):
        assert (
            _run(
                monkeypatch,
                capsys,
                "gh pr checks 5 --repo DanielH2018/dotfiles",
                "s7",
                tmp_path,
            )
            is None
        )
    assert _run(monkeypatch, capsys, "gh pr checks 1", "s7", tmp_path) is None


def test_empty_payload_is_ignored(monkeypatch, capsys):
    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO(""))
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""


def test_malformed_payload_is_ignored(monkeypatch, capsys):
    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO("{nope"))
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""
