"""Tests for the auto-approve-remote-ssh.sh PermissionRequest wrapper.

The wrapper is 15 lines and holds no classification logic — it execs
auto-approve-readonly.py with --permission-request, and that classifier has its own suite.
So these cover what the classifier's tests cannot see: the wiring between the two, and the
narrowing that this entry point exists to apply.

Why it needs its own tests at all. Every failure here is silent by construction. The hook's
documented posture is "no output -> the prompt stands", so a broken wrapper does not error —
it just stops approving, which is indistinguishable from a command that was never meant to be
approved. There is no log to notice.

Two layers, because CI and this host can check different things:

* Static checks run everywhere, including CI. They pin the contract between the wrapper and
  the classifier — the flag it passes, and the fail-open `|| exit 0` on the cd.
* End-to-end checks run the real script and skip when the machine-specific paths it hardcodes
  are absent. They are what proves the JSON on the wire is the shape Claude Code accepts.

test_setup_wiring.py already asserts that every .sh wrapper points at a .py that exists, so
that class is not repeated here.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent
WRAPPER = HOOKS / "auto-approve-remote-ssh.sh"
CLASSIFIER = HOOKS / "auto-approve-readonly.py"

WRAPPER_TEXT = WRAPPER.read_text(encoding="utf-8")

# The wrapper hardcodes these; it cannot run without them.
UV_BIN = Path("/home/ubuntu/.local/bin/uv")
REPO_DIR = Path("/home/ubuntu/server")

_runnable = pytest.mark.skipif(
    not (UV_BIN.exists() and REPO_DIR.is_dir() and shutil.which("bash")),
    reason="wrapper hardcodes /home/ubuntu paths and needs uv; not runnable here",
)


def run_hook(payload):
    """Feed the wrapper a PermissionRequest payload; return (exit code, stdout)."""
    proc = subprocess.run(
        ["bash", str(WRAPPER)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout.strip()


def request(command):
    return {"tool_input": {"command": command}}


def test_wrapper_passes_a_flag_the_classifier_recognises():
    """A renamed flag would leave the wrapper running and approving nothing, silently."""
    assert "--permission-request" in WRAPPER_TEXT
    assert '"--permission-request" in sys.argv' in CLASSIFIER.read_text(
        encoding="utf-8"
    )


def test_wrapper_fails_open_when_the_repo_is_missing():
    """cd must not be able to strand the hook:

    a failed cd has to exit 0, not run the exec from whatever directory it happened to land in.
    """
    assert "|| exit 0" in WRAPPER_TEXT, "cd has no fail-open guard"


@_runnable
def test_read_only_ssh_command_is_allowed():
    code, out = run_hook(request("ssh daniel-pi docker ps"))
    assert code == 0
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


@_runnable
def test_destructive_ssh_command_is_left_to_the_prompt():
    code, out = run_hook(request("ssh daniel-pi rm -rf /"))
    assert code == 0
    assert out == "", f"expected silence so the prompt stands, got {out!r}"


@_runnable
def test_local_read_only_command_is_not_answered_here():
    """The narrowing this entry point exists for.

    classify() calls `git status` read-only, but this hook speaks only for ssh traffic — answering
    more would resolve `ask` rules it was never meant to cover.
    """
    code, out = run_hook(request("git status"))
    assert code == 0
    assert out == ""


@_runnable
def test_ssh_with_a_dangerous_second_command_is_not_approved():
    """The newline-separator bypass, through the real wrapper rather than the classifier
    alone — see the shared corpus in test_command_vectors.py."""
    code, out = run_hook(request("ssh daniel-pi docker ps\nssh daniel-pi reboot"))
    assert code == 0
    assert out == ""


@_runnable
@pytest.mark.parametrize(
    "payload",
    ["", "not json at all", "{}", '{"tool_input": {}}'],
    ids=["empty", "malformed", "no-tool-input", "no-command"],
)
def test_unusable_input_fails_open(payload):
    """Anything unreadable must leave the prompt standing, never approve by default."""
    code, out = run_hook(payload)
    assert code == 0
    assert out == ""
