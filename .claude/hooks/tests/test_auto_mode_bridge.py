#!/usr/bin/env python3
"""Tests for the auto-mode-bridge hook.

Two events, two contracts:
  1. PostToolUseFailure — decode deploy.sh's resume-point exit codes, which all mean nothing was
     deployed and each of which has a different next step.
  2. PostToolUseFailure and PostToolUse on a `git push` — name the probe behind a ruleset
     refusal, and call a bypass notice a push that landed.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import io
import json
import os
import sys

import pytest

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auto-mode-bridge.py"
)
_spec = importlib.util.spec_from_file_location("auto_mode_bridge", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def failure(
    command="./scripts/deploy.sh --tags freshrss", error="Exit code 75\nlocked"
):
    return {
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "error": error,
    }


@pytest.mark.parametrize(
    "code, marker", [(75, "exit 75"), (4, "exit 4"), (3, "exit 3"), (2, "exit 2")]
)
def test_every_deploy_resume_point_is_decoded(code, marker):
    note = _mod.deploy_exit_note(failure(error=f"Exit code {code}\nsome output"))
    assert note is not None
    assert marker in note
    # The one fact all four share, and the one Claude gets wrong without it.
    assert "NOTHING was deployed" in note


def test_a_playbook_failure_is_decoded_as_changes_being_live():
    """The inverse of the four above, and the reason exit 20 exists.

    A play that applied its manifests and then failed must not be decoded as the tag miss,
    which would tell an operator nothing was deployed when everything before the failing task
    had been. The note must carry the opposite claim, in the words the wrapper and CLAUDE.md
    use.
    """
    note = _mod.deploy_exit_note(failure(error="Exit code 20\nsome output"))
    assert note is not None
    assert "exit 20" in note
    assert "changes ARE live" in note
    # The claim that must never reach an operator on this path.
    assert "NOTHING was deployed" not in note


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


def test_main_emits_the_documented_shape(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(failure())))
    _mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "exit 75" in out["hookSpecificOutput"]["additionalContext"]


def test_main_grants_no_retry_on_a_classifier_denial(capsys, monkeypatch):
    denial = {
        "hook_event_name": "PermissionDenied",
        "tool_name": "Bash",
        "tool_input": {"command": "./scripts/deploy_tools/gitops_tick.sh"},
        "reason": "Blocked by classifier",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(denial)))
    _mod.main()
    assert capsys.readouterr().out == ""


def test_main_says_nothing_on_an_unrelated_event(capsys, monkeypatch):
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "Stop"}))
    )
    _mod.main()
    assert capsys.readouterr().out == ""


def test_every_refusal_code_is_named_a_refusal_and_points_at_the_wrappers_own_lines():
    """`deploy_lib/run.py:report` prints the name, meaning and remedy, so what is left here is the
    framing an exit code cannot carry: this was a refusal, and the wrapper already said
    which one.
    """
    from lib.exit_codes import DEPLOY_SH_NO_VERDICT

    for rc in sorted(DEPLOY_SH_NO_VERDICT):
        note = _mod.deploy_exit_note(_deploy_failure(rc))
        assert f"deploy.sh exit {rc} is a refusal" in note
        assert "DEPLOY-VERDICT:" in note


def test_the_one_exit_where_changes_are_live_is_not_called_a_refusal():
    """The reject half: 20 means the playbook RAN."""
    from lib.exit_codes import DEPLOY_PLAYBOOK_FAILED

    note = _mod.deploy_exit_note(_deploy_failure(DEPLOY_PLAYBOOK_FAILED))
    assert "changes ARE live" in note
    assert "is a refusal" not in note


def test_the_codes_the_hook_holds_are_exactly_the_ones_the_contract_declares():
    """The hook keeps two integers of deploy.sh's contract and no prose. These are they."""
    from lib.exit_codes import DEPLOY_PLAYBOOK_FAILED, DEPLOY_SH_NO_VERDICT

    assert _mod._REFUSALS == DEPLOY_SH_NO_VERDICT
    assert _mod._PLAYBOOK_FAILED == DEPLOY_PLAYBOOK_FAILED
    assert _mod._PLAYBOOK_FAILED not in _mod._REFUSALS


def test_a_drifted_refusal_set_is_flagged():
    """Red proof for the check above, which can only ever be observed passing."""
    from lib.exit_codes import DEPLOY_SH_NO_VERDICT

    assert (_mod._REFUSALS - {75}) != DEPLOY_SH_NO_VERDICT, (
        "removing deploy.sh's lock-busy code must make the set disagree with the contract; "
        "if it does not, the comparison above is measuring nothing."
    )


def _deploy_failure(rc: int) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": "./scripts/deploy.sh --tags sonarr"},
        "error": f"Exit code {rc}\nsomething the harness printed",
    }


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
