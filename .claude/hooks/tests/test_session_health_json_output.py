#!/usr/bin/env python3
"""Tests for the SessionStart banner's JSON form: agent context versus operator message.

What the agent acts on goes in `hookSpecificOutput.additionalContext`; the operator's
housekeeping lines go in `systemMessage`, which the model does not see.

Run: uv run pytest .claude/hooks/tests/test_session_health_json_output.py
"""

import importlib.util
import json
import os

import pytest

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session-health.py"
)
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_TARGET = "  ✗ target loki [loki:3100] down"
_SESSION = "  • worktree-other-branch — k8s/roles/x/tasks/main.yml"
_UNLABELLED = "WARN: this worktree's name (bridge-abc123) was generated. Label it."
_REMOVABLE = "  ↳ worktree-merged-branch is merged and ready to remove"
_FANOUT = "  ↳ fan-out batch 1345 runs on daniel-server (worktree-fanout-1345)"


def _output(
    capsys,
    *,
    targets=(),
    sessions=(),
    unlabelled=(),
    worktrees=(),
    fanout=(),
):
    """Run main() with every probe faked, and return its stdout parsed as one JSON object."""
    assert (
        _mod.main(
            read_payload=lambda: {"source": "startup"},
            target_problems=lambda: list(targets),
            master_moved_problems=lambda: [],
            dirty_primary_problems=lambda: [],
            other_live_sessions=lambda cwd: list(sessions),
            unlabelled_worktree_lines=lambda cwd: list(unlabelled),
            stale_worktree_lines=lambda: list(worktrees),
            remote_fanout_lines=lambda: list(fanout),
        )
        == 0
    )
    out = capsys.readouterr().out
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        pytest.fail(f"session-health stdout is not one JSON object:\n{out}")
    assert isinstance(parsed, dict), out
    return parsed


def _context(parsed):
    return (parsed.get("hookSpecificOutput") or {}).get("additionalContext") or ""


def test_the_output_names_the_sessionstart_event(capsys):
    parsed = _output(capsys, targets=[_TARGET])
    assert parsed.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart"


def test_health_warn_lines_go_to_additional_context(capsys):
    parsed = _output(capsys, targets=[_TARGET])
    assert _TARGET in _context(parsed)
    assert _TARGET not in (parsed.get("systemMessage") or "")


def test_other_session_lines_go_to_additional_context(capsys):
    parsed = _output(capsys, sessions=[_SESSION])
    assert _SESSION in _context(parsed)
    assert _SESSION not in (parsed.get("systemMessage") or "")


def test_the_unlabelled_worktree_prompt_goes_to_additional_context(capsys):
    parsed = _output(capsys, unlabelled=[_UNLABELLED])
    assert _UNLABELLED in _context(parsed)
    assert _UNLABELLED not in (parsed.get("systemMessage") or "")


def test_removable_worktree_lines_go_to_the_system_message(capsys):
    parsed = _output(capsys, targets=[_TARGET], worktrees=[_REMOVABLE])
    assert _REMOVABLE in (parsed.get("systemMessage") or "")
    assert _REMOVABLE not in _context(parsed)


def test_fanout_lines_go_to_the_system_message(capsys):
    parsed = _output(capsys, targets=[_TARGET], fanout=[_FANOUT])
    assert _FANOUT in (parsed.get("systemMessage") or "")
    assert _FANOUT not in _context(parsed)


def test_operator_lines_alone_still_reach_the_system_message(capsys):
    parsed = _output(capsys, worktrees=[_REMOVABLE], fanout=[_FANOUT])
    message = parsed.get("systemMessage") or ""
    assert _REMOVABLE in message and _FANOUT in message
    assert _REMOVABLE not in _context(parsed) and _FANOUT not in _context(parsed)
