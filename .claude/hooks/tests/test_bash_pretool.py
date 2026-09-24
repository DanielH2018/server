#!/usr/bin/env python3
"""Tests for the one-process PreToolUse:Bash dispatcher (issue #2394).

Five hooks became five arms of one process, so the question these answer is whether each arm
still reaches its verdict THROUGH the dispatcher. Every arm therefore carries an accept/reject
pair driven through `main()` — the entry point the shim execs — and not through the arm's own
`decision()`, which the per-arm suites already cover. A dispatcher that returned every arm's
verdict and one that returned none look identical from the accepting side alone.

Two failures are specific to the merge and have their own tests: a command two arms both flag
must surface the higher decision with the earlier arm's reason, and an arm that raises must
lose its own verdict without taking the other four down with it.

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

pytestmark = pytest.mark.usefixtures("segmenter_or_skip")

# The arms this dispatcher must be running. Asserted by name rather than by count: the census
# is a tuple of filenames, and a renamed arm would otherwise leave every test below passing
# against four arms while the fifth judged nothing (`.claude/rules/python-layout.md`).
EXPECTED_ARMS = (
    "auto-approve-readonly",
    "block-protected-bash",
    "nudge-land-sh",
    "block-footguns",
    "inject-nested-docs",
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A loader that returns the real arms with their per-session state under tmp_path.

    Each arm is loaded fresh inside `collect`, so there is no module object to monkeypatch
    from here. Patching the freshly loaded one is the same fix one level in: `nudge-land-sh`
    counts CI reads in a file under the temp dir, and `inject-nested-docs` keys its
    once-per-session state the same way and appends a row to the instructions log.

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


def dispatch(command, load, monkeypatch, capsys, cwd=_REPO):
    """The dispatcher's parsed output for `command`, or None when it emitted nothing."""
    payload = {
        "tool_name": "Bash",
        "cwd": cwd,
        "session_id": f"test-{uuid.uuid4()}",
        "tool_input": {"command": command},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main(load=load) == 0
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"] if out.strip() else None


def test_the_arm_census_is_the_five_hooks_that_were_merged():
    names = tuple(name for name, _ in _mod._DECISION_ARMS) + (_mod._CONTEXT_ARM[0],)
    assert names == EXPECTED_ARMS
    for name in names:
        assert os.path.exists(os.path.join(_HOOKS, f"{name}.py")), name


# ── arm 1: auto-approve-readonly ─────────────────────────────────────────────────────


def test_accept_a_read_only_command_is_allowed(sandbox, monkeypatch, capsys):
    out = dispatch("ls -la", sandbox, monkeypatch, capsys)
    assert out["permissionDecision"] == "allow"
    assert "read-only" in out["permissionDecisionReason"]


def test_reject_a_writing_command_gets_no_allow(sandbox, monkeypatch, capsys):
    out = dispatch("rm -rf /tmp/nothing-here", sandbox, monkeypatch, capsys)
    assert out is None


# ── arm 2: block-protected-bash ──────────────────────────────────────────────────────


def test_accept_a_write_to_a_sops_file_asks(sandbox, monkeypatch, capsys):
    out = dispatch(
        "sed -i s/a/b/ ansible/vars/secrets.yml", sandbox, monkeypatch, capsys
    )
    assert out["permissionDecision"] == "ask"
    assert "SOPS-encrypted" in out["permissionDecisionReason"]


def test_reject_a_write_to_an_ordinary_file_is_not_asked(sandbox, monkeypatch, capsys):
    out = dispatch("sed -i s/a/b/ README.md", sandbox, monkeypatch, capsys)
    assert out is None


# ── arm 3: nudge-land-sh ─────────────────────────────────────────────────────────────


def test_accept_a_blocking_ci_wait_is_denied(sandbox, monkeypatch, capsys):
    out = dispatch("gh run watch 12345", sandbox, monkeypatch, capsys)
    assert out["permissionDecision"] == "deny"
    assert "land.sh" in out["permissionDecisionReason"]


def test_reject_a_first_ci_status_read_is_not_denied(sandbox, monkeypatch, capsys):
    out = dispatch("gh pr checks 620", sandbox, monkeypatch, capsys)
    assert out is None


# ── arm 4: block-footguns ────────────────────────────────────────────────────────────


def test_accept_a_ugrep_null_flag_is_denied(sandbox, monkeypatch, capsys):
    out = dispatch("grep -lZ needle .", sandbox, monkeypatch, capsys)
    assert out["permissionDecision"] == "deny"
    assert "ugrep" in out["permissionDecisionReason"]


def test_reject_the_same_grep_without_the_flag_is_not_denied(
    sandbox, monkeypatch, capsys
):
    out = dispatch("grep -l needle .", sandbox, monkeypatch, capsys)
    assert out["permissionDecision"] == "allow"


# ── arm 5: inject-nested-docs ────────────────────────────────────────────────────────


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


def test_a_deny_outranks_an_allow_on_the_same_command(sandbox, monkeypatch, capsys):
    """`grep -lZ` is read-only to arm 1 and a footgun to arm 4. Five separate hooks let the
    harness rank them; one process has to do it here, and ranking it the other way would
    auto-approve the exact command the footgun guard exists to stop."""
    out = dispatch("grep -lZ needle .", sandbox, monkeypatch, capsys)
    assert out["permissionDecision"] == "deny"


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
    """The injector used to be its own hook, so a denied command still got its docs. One
    object carries both keys; dropping the context on a deny would be a silent loss."""
    out = dispatch(
        "grep -lZ needle ansible/roles/k8s/home-assistant/tasks/main.yml",
        sandbox,
        monkeypatch,
        capsys,
    )
    assert out["permissionDecision"] == "deny"
    assert "ansible/roles/k8s/home-assistant/CLAUDE.md" in out["additionalContext"]


# ── one arm failing ──────────────────────────────────────────────────────────────────


def test_an_arm_that_raises_loses_only_its_own_verdict(sandbox, monkeypatch, capsys):
    """The failure this file is most able to hide: four clean verdicts and one dead arm."""

    def load(filename, module_name):
        if filename == "block-footguns":
            raise RuntimeError("arm is broken")
        return sandbox(filename, module_name)

    payload = {
        "tool_name": "Bash",
        "cwd": _REPO,
        "session_id": f"test-{uuid.uuid4()}",
        "tool_input": {"command": "grep -lZ needle ."},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main(load=load) == 0
    captured = capsys.readouterr()
    # Arm 4's deny is gone, but arm 1 still judged the same command.
    assert (
        json.loads(captured.out)["hookSpecificOutput"]["permissionDecision"] == "allow"
    )
    # And it said so, naming the arm: a dead arm behind four clean verdicts must not be silent.
    assert "block-footguns" in captured.err
    assert "arm is broken" in captured.err
