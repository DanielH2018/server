#!/usr/bin/env python3
"""Tests for the one-process PreToolUse:Bash dispatcher.

The hooks are arms of one process, so the question these answer is whether each arm still
reaches its verdict THROUGH the dispatcher. Every arm therefore carries an accept/reject pair
driven through `main()` — the entry point the shim execs — and not through the arm's own
`decision()`, which the per-arm suites already cover. A dispatcher that returned every arm's
verdict and one that returned none look identical from the accepting side alone.

Two failures are specific to the merge and have their own tests: a command two arms both flag
must surface the higher decision with the earlier arm's reason, and an arm that raises must
lose its own verdict without taking the others down with it.

Run: uv run pytest .claude/hooks/tests/test_bash_pretool.py
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import uuid

import pytest

_HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_HOOKS))
sys.path.insert(0, _HOOKS)  # the arms import _hook_common


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), os.path.join(_HOOKS, f"{name}.py")
    )
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load("bash-pretool")

# The arms this dispatcher must be running. Asserted by name rather than by count: the census
# is a tuple of filenames, and a renamed arm would otherwise leave every test below passing
# against four arms while the fifth judged nothing (`.claude/rules/python-layout.md`).
EXPECTED_ARMS = (
    "block-protected-bash",
    "block-footguns",
    "inject-nested-docs",
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A loader that returns the real arms with their per-session state under tmp_path.

    Each arm is loaded fresh inside `collect`, so there is no module object to monkeypatch
    from here. Patching the freshly loaded one is the same fix one level in: `inject-nested-docs`
    keys its once-per-session state in a file under the temp dir and appends a row to the
    instructions log.

    Every arm reads the same stdlib `tempfile`, so one patch on the singleton redirects all of
    them — and it goes through `monkeypatch` because a plain assignment would leave every later
    `gettempdir()` in this xdist worker pointing at a torn-down `tmp_path`. `_logger` IS
    per-arm, since `load_arm` execs a fresh `log_instructions` for each call.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    def load(filename, module_name):
        module = _mod.load_arm(filename, module_name)
        if hasattr(module, "_logger"):
            module._logger.LOG = str(tmp_path / "instructions.log")
        return module

    return load


def dispatch(command, load, monkeypatch, capsys, cwd=_REPO, **tool_input):
    """The dispatcher's parsed output for `command`, or None when it emitted nothing.

    Extra keyword arguments ride in `tool_input` beside the command, the way the harness sends
    `run_in_background`, `timeout` and `description`.
    """
    payload = {
        "tool_name": "Bash",
        "cwd": cwd,
        "session_id": f"test-{uuid.uuid4()}",
        "tool_input": {"command": command, **tool_input},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main(load=load) == 0
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"] if out.strip() else None


def test_the_arm_census_is_the_hooks_that_were_merged():
    names = tuple(name for name, _ in _mod._DECISION_ARMS) + (_mod._CONTEXT_ARM[0],)
    assert names == EXPECTED_ARMS
    for name in names:
        assert os.path.exists(os.path.join(_HOOKS, f"{name}.py")), name


# ── arm 2: block-protected-bash ──────────────────────────────────────────────────────


def test_accept_a_write_to_a_sops_file_asks(sandbox, monkeypatch, capsys):
    out = dispatch(
        "sed -i s/a/b/ ansible/vars/secrets.yml", sandbox, monkeypatch, capsys
    )
    assert out["permissionDecision"] == "ask"
    assert out["permissionDecisionReason"].startswith("[block-protected-bash] ")
    assert "SOPS-encrypted" in out["permissionDecisionReason"]


def test_reject_a_write_to_an_ordinary_file_is_not_asked(sandbox, monkeypatch, capsys):
    out = dispatch("sed -i s/a/b/ README.md", sandbox, monkeypatch, capsys)
    assert out is None


# ── arm 3: block-footguns ────────────────────────────────────────────────────────────


def test_accept_a_rollout_restart_is_denied(sandbox, monkeypatch, capsys):
    out = dispatch("kubectl rollout restart deploy/x", sandbox, monkeypatch, capsys)
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("[block-footguns] ")
    assert "deploy.sh" in out["permissionDecisionReason"]


def test_reject_a_rollout_status_is_not_denied(sandbox, monkeypatch, capsys):
    out = dispatch("kubectl rollout status deploy/x", sandbox, monkeypatch, capsys)
    assert out is None or out["permissionDecision"] != "deny"


# ── arm 4: inject-nested-docs ────────────────────────────────────────────────────────


def test_accept_a_command_naming_a_role_file_injects_its_docs(
    sandbox, monkeypatch, capsys
):
    out = dispatch(
        "cat ansible/roles/k8s/home-assistant/tasks/main.yml",
        sandbox,
        monkeypatch,
        capsys,
    )
    assert "ansible/roles/k8s/home-assistant/CLAUDE.md" in out["additionalContext"]


def test_reject_a_command_naming_no_path_injects_nothing(sandbox, monkeypatch, capsys):
    out = dispatch("git status", sandbox, monkeypatch, capsys)
    assert out is None or "additionalContext" not in out


# ── the merge ────────────────────────────────────────────────────────────────────────


def test_the_earliest_arm_at_the_winning_level_keeps_the_reason():
    """What the harness does across hooks: the first deny wins the surfaced reason."""
    verdicts = [("allow", "first"), ("deny", "from arm 2"), ("deny", "from arm 4")]
    assert _mod.merge(verdicts) == ("deny", "from arm 2")


def test_an_ask_outranks_an_allow_and_loses_to_a_deny():
    assert _mod.merge([("allow", "a"), ("ask", "b")]) == ("ask", "b")
    assert _mod.merge([("ask", "b"), ("deny", "c")]) == ("deny", "c")


def test_no_verdict_from_any_arm_emits_nothing():
    assert _mod.merge([]) == (None, None)


def test_a_deny_still_carries_the_context_arm_s_injection(sandbox, monkeypatch, capsys):
    """A denied command still gets its docs. One
    object carries both keys; dropping the context on a deny would be a silent loss."""
    out = dispatch(
        "ssh daniel-server 'git log -1 ansible/roles/k8s/home-assistant/tasks/main.yml'",
        sandbox,
        monkeypatch,
        capsys,
    )
    assert out["permissionDecision"] == "deny"
    assert "ansible/roles/k8s/home-assistant/CLAUDE.md" in out["additionalContext"]


# ── the rewrite ──────────────────────────────────────────────────────────────────────


def test_accept_a_rewrite_keeps_the_rest_of_the_tool_input(
    sandbox, monkeypatch, capsys
):
    """The harness replaces the input with `updatedInput`, so a key left out is a key lost.

    Sending `command` alone ran a backgrounded call inline under the 120s default (#3501).
    """
    out = dispatch(
        "pytest --version",
        sandbox,
        monkeypatch,
        capsys,
        run_in_background=True,
        timeout=600000,
        description="Print the pytest version",
    )
    assert out["updatedInput"] == {
        "command": "uv run pytest --version",
        "run_in_background": True,
        "timeout": 600000,
        "description": "Print the pytest version",
    }


def test_reject_a_cc_wait_call_is_not_rewritten(sandbox, monkeypatch, capsys):
    """A wait's `run_in_background` belongs to the user-level guard, which sets it.

    A second hook's rewrite of the same call would replace the guard's input wholesale.
    """
    out = dispatch(
        "cc-wait land 3501", sandbox, monkeypatch, capsys, run_in_background=True
    )
    assert out is None or "updatedInput" not in out


# ── one arm failing ──────────────────────────────────────────────────────────────────


def test_an_arm_that_raises_loses_only_its_own_verdict(sandbox, monkeypatch, capsys):
    """The failure this file is most able to hide: clean verdicts and one dead arm."""

    def load(filename, module_name):
        if filename == "block-protected-bash":
            raise RuntimeError("arm is broken")
        return sandbox(filename, module_name)

    payload = {
        "tool_name": "Bash",
        "cwd": _REPO,
        "session_id": f"test-{uuid.uuid4()}",
        "tool_input": {"command": "kubectl rollout restart deploy/x"},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main(load=load) == 0
    captured = capsys.readouterr()
    # Arm 2 is gone, but arm 4 still judged the same command.
    assert (
        json.loads(captured.out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    # And it said so, naming the arm: a dead arm behind clean verdicts must not be silent.
    assert "block-protected-bash" in captured.err
    assert "arm is broken" in captured.err
