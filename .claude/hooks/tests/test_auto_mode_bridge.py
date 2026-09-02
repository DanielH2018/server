#!/usr/bin/env python3
"""Tests for the auto-mode-bridge hook.

Two events, two contracts:
  1. PermissionDenied — retry the gitops tick's known classifier flake, at most twice, and only
     when the classifier actually reached a verdict.
  2. PostToolUseFailure — decode deploy.sh's resume-point exit codes, which all mean nothing was
     deployed and each of which has a different next step.

The retry ledger writes under `.claude/logs/`, so every test that can grant a retry points
`ledger_path` at a tmp_path instead — otherwise the suite's own runs would spend the cap and the
later cases would pass for the wrong reason.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import json
import os

import pytest

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auto-mode-bridge.py"
)
_spec = importlib.util.spec_from_file_location("auto_mode_bridge", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

TICK = "./scripts/deploy_tools/gitops_tick.sh"


def denial(command=TICK, reason="Blocked by classifier", tool="Bash"):
    return {
        "hook_event_name": "PermissionDenied",
        "tool_name": tool,
        "tool_input": {"command": command},
        "reason": reason,
        "session_id": "test-session",
    }


def failure(
    command="./scripts/deploy.sh --tags freshrss", error="Exit code 75\nlocked"
):
    return {
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "error": error,
    }


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Point the retry ledger at a tmp file, and hand back its path."""
    path = str(tmp_path / "retries.json")
    monkeypatch.setattr(_mod, "ledger_path", lambda _session: path)
    return path


@pytest.mark.parametrize(
    "command",
    [
        TICK,
        "scripts/deploy_tools/gitops_tick.sh",
        "/home/ubuntu/server/scripts/deploy_tools/gitops_tick.sh",
        "cd /home/ubuntu/server && ./scripts/deploy_tools/gitops_tick.sh",
        f"{TICK}  ",
    ],
)
def test_retries_the_tick_however_it_is_spelled(command, ledger):
    assert _mod.should_retry(denial(command=command)) is True


@pytest.mark.parametrize(
    "command",
    [
        # The classifier judged the WHOLE line, and the half it objected to may be the other
        # one — so a tick riding along in a compound command gets no free retry.
        f"{TICK} && ./scripts/deploy.sh --tags freshrss",
        f"rm -rf /srv/data; {TICK}",
        f"{TICK} | tee /tmp/tick.log",
        "./scripts/deploy.sh --changed HEAD~1",
    ],
)
def test_does_not_retry_anything_else(command, ledger):
    assert _mod.should_retry(denial(command=command)) is False


@pytest.mark.parametrize(
    "reason",
    [
        "Auto mode could not evaluate this action and is blocking it for safety",
        "Classifier unavailable",
    ],
)
def test_does_not_retry_a_denial_the_classifier_never_adjudicated(reason, ledger):
    assert _mod.should_retry(denial(reason=reason)) is False
    # ...and the refusal costs nothing, so a later real denial still has its full budget.
    assert _mod.retries_used(ledger) == 0


def test_a_non_bash_denial_is_not_ours(ledger):
    assert _mod.should_retry(denial(tool="Agent")) is False


def test_the_cap_stops_a_retry_loop(ledger):
    granted = [_mod.should_retry(denial()) for _ in range(4)]
    assert granted == [True, True, False, False]
    assert _mod.retries_used(ledger) == _mod.MAX_RETRIES_PER_SESSION


def test_a_malformed_ledger_reads_as_zero_rather_than_disabling_the_hook(ledger):
    with open(ledger, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert _mod.retries_used(ledger) == 0
    assert _mod.should_retry(denial()) is True


@pytest.mark.parametrize(
    "code, marker", [(75, "exit 75"), (4, "exit 4"), (3, "exit 3"), (2, "exit 2")]
)
def test_every_deploy_resume_point_is_decoded(code, marker):
    note = _mod.deploy_exit_note(failure(error=f"Exit code {code}\nsome output"))
    assert note is not None
    assert marker in note
    # The one fact all four share, and the one Claude gets wrong without it.
    assert "NOTHING was deployed" in note


@pytest.mark.parametrize(
    "payload",
    [
        # A real playbook failure: exit 1 is not a resume point and must stay unannotated.
        failure(error="Exit code 1\nfatal: task failed"),
        # Some other command's failure.
        failure(command="uv run pytest", error="Exit code 75\n"),
        # Shell never started, so there is no exit-code line to key on.
        failure(error="Command not found"),
        # An abort is not the wrapper refusing.
        {**failure(), "is_interrupt": True},
    ],
)
def test_leaves_everything_else_alone(payload):
    assert _mod.deploy_exit_note(payload) is None


def test_the_deploy_note_survives_a_compound_invocation():
    note = _mod.deploy_exit_note(
        failure(command="cd /home/ubuntu/server && ./scripts/deploy.sh --tags sonarr")
    )
    assert note is not None


def test_main_emits_the_documented_shape(capsys, ledger):
    _mod.main.__globals__["sys"].stdin = _StringIO(json.dumps(denial()))
    _mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "hookSpecificOutput": {"hookEventName": "PermissionDenied", "retry": True}
    }


def test_main_says_nothing_on_an_unrelated_event(capsys):
    _mod.main.__globals__["sys"].stdin = _StringIO(
        json.dumps({"hook_event_name": "Stop"})
    )
    _mod.main()
    assert capsys.readouterr().out == ""


class _StringIO:
    """Minimal stdin stand-in: json.load only needs read()."""

    def __init__(self, text):
        self._text = text

    def read(self, *_args):
        return self._text
