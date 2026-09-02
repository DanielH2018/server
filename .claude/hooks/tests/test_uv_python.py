"""Tests for the uv-python.sh PreToolUse rewrite hook.

The hook exists because this repo is 3.14-only and uses PEP 758 syntax that Ubuntu's
3.12 /usr/bin/python3 cannot parse. A bare `pytest` therefore fails with a SyntaxError
that names a repo file, which reads as a repo bug. The rewrite makes the documented
`uv run` rule structural.

Every failure of this hook is silent by construction: its posture is "no output -> the
command stands", so a broken rewrite does not error, it just stops rewriting — which is
indistinguishable from a command that was never meant to be rewritten. Hence a corpus
rather than a smoke test.

Two properties matter more than any single vector:

* **Idempotence.** `uv run pytest` must not become `uv run uv run pytest`. The hook gets
  this from its pattern (a wrapped segment's first word is `uv`), not from a check, so a
  pattern edit can lose it without any other test noticing.
* **Quoted text is never spliced.** `;`/`&&`/`|` separate commands outside quotes and
  separate nothing inside them, so the hook tracks quote state rather than pattern-matching
  the characters. Losing that turns `python3 -c 'a; python3 b'` into a corrupted program —
  while a guard crude enough to bail on any quote at all would miss
  `cd "$HOME/server" && pytest`, the most common multi-segment invocation in a worktree.
  Both directions are covered below.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent
HOOK = HOOKS / "uv-python.sh"

HOOK_TEXT = HOOK.read_text(encoding="utf-8")

_runnable = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("jq")),
    reason="hook needs bash and jq",
)


def rewrite(command, tool_name="Bash"):
    """Feed the hook a PreToolUse payload; return the rewritten command, or None."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    proc = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    return out["hookSpecificOutput"]["updatedInput"]["command"]


# --- static: the fail-open posture --------------------------------------------------------


def test_hook_exits_silently_without_jq():
    """No jq means no parse; the command must stand rather than be mangled."""
    assert "command -v jq >/dev/null 2>&1 || exit 0" in HOOK_TEXT


def test_hook_documents_why_it_rewrites_rather_than_pins_a_path():
    """A PATH pin at the primary checkout's .venv would cross worktrees silently."""
    assert "uv run` resolves the venv from the *caller's* working" in HOOK_TEXT


def test_hook_bails_on_an_unterminated_quote():
    """Losing quote state makes every later offset a guess; splicing on a guess is worse
    than not rewriting."""
    assert '[[ "$state" == none ]] || exit 0' in HOOK_TEXT


# --- the programs that must be routed -----------------------------------------------------


@_runnable
@pytest.mark.parametrize(
    "command,expected",
    [
        ("pytest", "uv run pytest"),
        ("pytest ansible/tests", "uv run pytest ansible/tests"),
        ("py.test", "uv run py.test"),
        ("python -V", "uv run python -V"),
        ("python3 -m pytest", "uv run python3 -m pytest"),
        (
            "ansible-playbook ansible/deploy.yml --check",
            "uv run ansible-playbook ansible/deploy.yml --check",
        ),
        # A shebang-invoked script names no interpreter, so nothing python-shaped
        # appears in the command at all — it would otherwise reach 3.12 unnoticed.
        (
            "./scripts/diagnostics/probe.py targets",
            "uv run ./scripts/diagnostics/probe.py targets",
        ),
        (
            "scripts/deploy_tools/deploy_tags.py --list",
            "uv run scripts/deploy_tools/deploy_tags.py --list",
        ),
    ],
)
def test_bare_invocations_are_routed_through_uv(command, expected):
    assert rewrite(command) == expected


@_runnable
def test_rewrite_applies_after_a_separator():
    assert rewrite("cd ansible && pytest tests") == "cd ansible && uv run pytest tests"


@_runnable
def test_rewrite_applies_to_a_pipeline_consumer():
    assert rewrite("cat data.json | python3 -") == "cat data.json | uv run python3 -"


# --- what must be left alone --------------------------------------------------------------


@_runnable
@pytest.mark.parametrize(
    "command",
    [
        # Idempotence: the property the pattern must never lose.
        "uv run pytest",
        "uv run python scripts/diagnostics/probe.py targets",
        "uv run --no-sync python foo.py",
        # Not a segment start — these are arguments, not programs.
        "which python3",
        "command -v pytest",
        "cat scripts/diagnostics/probe.py",
        "grep -n pytest prek.toml",
        # Nothing python-shaped at all.
        "ls -la",
        "git status",
    ],
)
def test_unaffected_commands_are_left_untouched(command):
    assert rewrite(command) is None


@_runnable
def test_non_bash_tools_are_ignored():
    assert rewrite("pytest", tool_name="Edit") is None


# --- quoting: the splice this hook must not perform ---------------------------------------


@_runnable
def test_quoted_program_text_is_never_spliced():
    """The `;` here is inside the -c program, not a command separator.

    Rewriting at it would insert `uv run` into the Python source and change what the command means.
    """
    out = rewrite("""python3 -c 'a = 1; python3 = 2'""")
    assert out == """uv run python3 -c 'a = 1; python3 = 2'"""


@_runnable
def test_leading_program_is_still_rewritten_when_arguments_are_quoted():
    assert rewrite("pytest -k 'retry'") == "uv run pytest -k 'retry'"


@_runnable
def test_a_quoted_cd_prefix_still_reaches_the_test_command():
    """The shape a worktree session actually types.

    A guard that bailed on the mere presence of a quote would leave this `pytest` bare — the exact
    failure the hook exists to prevent, and silently.
    """
    assert (
        rewrite('cd "$HOME/server" && pytest') == 'cd "$HOME/server" && uv run pytest'
    )


@_runnable
def test_a_separator_inside_a_quoted_argument_is_not_a_segment_start():
    assert rewrite("""echo 'a && pytest' """) is None


@_runnable
def test_a_command_substitution_separator_is_a_real_separator():
    """`;` inside `$(...)` genuinely separates commands, so rewriting there is correct."""
    assert rewrite("echo $(cd x; pytest)") == "echo $(cd x; uv run pytest)"


@_runnable
def test_an_unterminated_quote_leaves_the_command_alone():
    assert rewrite("""python3 -c 'unterminated && pytest""") is None
