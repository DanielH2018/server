#!/usr/bin/env python3
"""Tests for the auto-mode-bridge hook.

PostToolUseFailure and PostToolUse on a `git push`: name the probe behind a ruleset refusal,
and call a bypass notice a push that landed.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import io
import json
import os
import sys


_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auto-mode-bridge.py"
)
_spec = importlib.util.spec_from_file_location("auto_mode_bridge", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def failure(command="git push origin master", error="Exit code 1\nrefused"):
    return {
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "error": error,
    }


def test_main_says_nothing_on_an_unrelated_event(capsys, monkeypatch):
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "Stop"}))
    )
    _mod.main()
    assert capsys.readouterr().out == ""


# GitHub's output for a ruleset refusal and for a bypass, as a push prints them. The bypass shape
# is copied from a session transcript; both print the same `- Cannot update this protected ref.`.
REFUSED_PUSH = (
    "Exit code 1\n"
    "remote: error: GH013: Repository rule violations found for refs/heads/master.\n"
    "remote: - Cannot update this protected ref.\n"
    " ! [remote rejected] master -> master (push declined due to repository rule violations)\n"
)
BYPASSED_PUSH = (
    "remote: Bypassed rule violations for refs/heads/worktree-fanout-4507:        \n"
    "remote: - Cannot update this protected ref.        \n"
)


def test_a_ruleset_refused_push_names_the_probe_for_its_branch():
    note = _mod.push_refusal_note(
        failure(command="git push origin master", error=REFUSED_PUSH)
    )
    assert "probe.py push-rules master`" in note


def test_a_push_that_failed_for_another_reason_gets_no_note():
    error = "Exit code 128\nfatal: unable to access 'https://github.com/': Could not resolve host"
    assert _mod.push_refusal_note(failure(command="git push", error=error)) is None
    assert (
        _mod.push_refusal_note(failure(command="git status", error=REFUSED_PUSH))
        is None
    )


def _push_success(stderr, command="git push -u origin worktree-fanout-4507"):
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": "", "stderr": stderr},
    }


def test_a_bypass_notice_is_named_a_push_that_landed(capsys, monkeypatch):
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(_push_success(BYPASSED_PUSH)))
    )
    _mod.main()
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    assert "the push landed" in out["additionalContext"]
    assert "push-rules worktree-fanout-4507`" in out["additionalContext"]


def test_a_push_with_no_bypass_notice_gets_no_note():
    plain = "To github.com:DanielH2018/server.git\n   abc..def  topic -> topic\n"
    assert _mod.push_bypass_note(_push_success(plain)) is None
