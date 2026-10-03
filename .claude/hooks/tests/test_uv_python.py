"""Tests for the uv-python PreToolUse rewrite arm.

The arm exists because this repo is 3.14-only and uses PEP 758 syntax that Ubuntu's
3.12 /usr/bin/python3 cannot parse. A bare `pytest` therefore fails with a SyntaxError
that names a repo file, which reads as a repo bug. The rewrite makes the documented
`uv run` rule structural.

Every failure of this arm is silent by construction: its posture is "no rewrite -> the
command stands", so a broken rewrite does not error, it just stops rewriting — which is
indistinguishable from a command that was never meant to be rewritten. Hence a corpus
rather than a smoke test.

Two properties matter more than any single vector:

* **Idempotence.** `uv run pytest` must not become `uv run uv run pytest`. The arm gets
  this from its pattern (a wrapped segment's first word is `uv`), not from a check, so a
  pattern edit can lose it without any other test noticing.
* **Quoted text is never spliced.** `;`/`&&`/`|` separate commands outside quotes and
  separate nothing inside them, so the arm tracks quote state rather than pattern-matching
  the characters. Losing that turns `python3 -c 'a; python3 b'` into a corrupted program —
  while a guard crude enough to bail on any quote at all would miss
  `cd "$HOME/server" && pytest`, the most common multi-segment invocation in a worktree.
  Both directions are covered below.

Until #3286 this was `uv-python.sh`, its own PreToolUse hook in 269 lines of bash and jq, and
the corpus below drove it through `bash`. It is now the fifth arm of `bash-pretool.py`, so the
corpus calls `rewrite_command` directly and `test_the_dispatcher_emits_the_rewrite` covers the
one thing that only the dispatcher can get wrong.

Run: uv run pytest .claude/hooks/tests/test_uv_python.py
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import uuid

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(HOOKS))
sys.path.insert(0, HOOKS)  # the sibling arms import _hook_common


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), os.path.join(HOOKS, f"{name}.py")
    )
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


arm = _load("uv-python")

# The prefix the arm puts in front of an ansible command when `stdio-blocking` (the
# dotfiles repo's standalone fixup binary) is not on PATH. Kept verbatim rather than
# re-derived: a change to what the arm prepends should fail here and be read, since the
# ansible CLIs refuse to start when it is missing.
STDIO_FIXUP_FALLBACK = "python3 -c 'import os; [os.set_blocking(f, True) for f in (0, 1, 3)]' 3>&2 2>/dev/null; "

# The prefix once `stdio-blocking` has deployed. This is the preferred form: it does not
# match the dotfiles repo's `Bash(python3 -c:*)` ask rule, which the fallback's inline
# `python3 -c` does.
STDIO_FIXUP_STDIO_BLOCKING = "stdio-blocking; "


def _absent(_name):
    """A `which` that resolves nothing, so the inline fallback branch is the one measured."""
    return None


def _present(name):
    """A `which` that resolves `stdio-blocking`, standing on a host the dotfiles reached."""
    return (
        "/home/ubuntu/.local/bin/stdio-blocking" if name == "stdio-blocking" else None
    )


def rewrite(command, tool_name="Bash", which=_absent):
    """The arm's rewritten command for a PreToolUse payload, or None."""
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    return arm.rewrite(payload, which=which)


# --- the fail-open posture ----------------------------------------------------------------
#
# The old shell hook asserted its three bail-outs by grepping its own text. The arm is Python,
# so each is measured instead: a refusal is a None return, and None means the command stands.


def test_an_unreadable_payload_leaves_the_command_alone():
    assert arm.rewrite({}) is None
    assert arm.rewrite({"tool_name": "Bash", "tool_input": {}}) is None
    assert arm.rewrite({"tool_name": "Bash", "tool_input": {"command": ""}}) is None


def test_the_arm_rewrites_rather_than_pinning_a_venv_path():
    """A PATH pin at the primary checkout's .venv would cross worktrees silently, so the
    rewrite names `uv run` and never a venv directory."""
    out = rewrite("pytest")
    assert out == "uv run pytest"
    assert ".venv" not in out


def test_an_unterminated_quote_yields_no_command_starts():
    """Losing quote state makes every later offset a guess; splicing on a guess is worse
    than not rewriting. The pair: a balanced command does yield starts."""
    assert arm.command_starts("python3 -c 'unterminated && pytest") is None
    assert arm.command_starts("pytest; pytest") == [0, 7]


# --- the programs that must be routed -----------------------------------------------------


@pytest.mark.parametrize(
    "command,expected",
    [
        ("pytest", "uv run pytest"),
        ("pytest ansible/tests", "uv run pytest ansible/tests"),
        ("py.test", "uv run py.test"),
        ("python -V", "uv run python -V"),
        ("python3 -m pytest", "uv run python3 -m pytest"),
        # ansible carries the stdio fixup too; the pair below owns that half. `which` resolves
        # nothing here so this pins the fallback prefix specifically.
        (
            "ansible-playbook ansible/deploy.yml --check",
            STDIO_FIXUP_FALLBACK + "uv run ansible-playbook ansible/deploy.yml --check",
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


def test_rewrite_applies_after_a_separator():
    assert rewrite("cd ansible && pytest tests") == "cd ansible && uv run pytest tests"


def test_rewrite_applies_to_a_pipeline_consumer():
    assert rewrite("cat data.json | python3 -") == "cat data.json | uv run python3 -"


# A `VAR=x.py` word is a shell assignment, not a script invocation. The arm once rewrote it
# to `uv run P=…`, which failed with `Failed to spawn: P=…`. The pair proves the fix
# skips only the assignment rather than abandoning the whole command.


def test_a_program_after_a_py_valued_assignment_is_rewritten():
    assert rewrite('P=x.py; python3 "$P"') == 'P=x.py; uv run python3 "$P"'


def test_a_py_valued_assignment_is_left_alone():
    assert rewrite('P=path/to/x.py; sed -n 1p "$P"') is None


# --- what must be left alone --------------------------------------------------------------


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


def test_non_bash_tools_are_ignored():
    assert rewrite("pytest", tool_name="Edit") is None


# --- quoting: the splice this arm must not perform -----------------------------------------


def test_quoted_program_text_is_never_spliced():
    """The `;` here is inside the -c program, not a command separator.

    Rewriting at it would insert `uv run` into the Python source and change what the command means.
    """
    out = rewrite("""python3 -c 'a = 1; python3 = 2'""")
    assert out == """uv run python3 -c 'a = 1; python3 = 2'"""


def test_leading_program_is_still_rewritten_when_arguments_are_quoted():
    assert rewrite("pytest -k 'retry'") == "uv run pytest -k 'retry'"


def test_a_quoted_cd_prefix_still_reaches_the_test_command():
    """The shape a worktree session actually types.

    A guard that bailed on the mere presence of a quote would leave this `pytest` bare — the exact
    failure the arm exists to prevent, and silently.
    """
    assert (
        rewrite('cd "$HOME/server" && pytest') == 'cd "$HOME/server" && uv run pytest'
    )


def test_a_separator_inside_a_quoted_argument_is_not_a_segment_start():
    assert rewrite("""echo 'a && pytest' """) is None


def test_a_command_substitution_separator_is_a_real_separator():
    """`;` inside `$(...)` genuinely separates commands, so rewriting there is correct."""
    assert rewrite("echo $(cd x; pytest)") == "echo $(cd x; uv run pytest)"


def test_an_unterminated_quote_leaves_the_command_alone():
    assert rewrite("""python3 -c 'unterminated && pytest""") is None


# --- the blocking-stdio fixup -------------------------------------------------------------
#
# Claude Code's Bash tool hands its child stdout and stderr with O_NONBLOCK set, and every
# ansible CLI calls check_blocking_io() at import time and exits rather than run. The arm
# prepends a restore to the commands that name one. Below: commands that must carry it,
# commands that must not, and a functional check with its own control.


@pytest.mark.parametrize(
    "command",
    [
        "ansible-playbook ansible/deploy.yml --tags jellyfin",
        "ansible --version",
        "ansible-inventory --list",
        # Already `uv run`, so nothing is rewritten and only the fixup fires.
        "uv run ansible-playbook ansible/bootstrap.yml",
        "uv run ansible-vault view ansible/vars/secrets.yml",
        "uv run --frozen ansible-playbook ansible/deploy.yml",
        # Not the first segment: the flag is shared, so one clear at the front covers it.
        'cd "$HOME/server" && ansible-playbook ansible/deploy.yml',
    ],
)
def test_ansible_commands_carry_the_stdio_fixup(command):
    out = rewrite(command)
    assert out is not None, command
    assert out.startswith(STDIO_FIXUP_FALLBACK), out


@pytest.mark.parametrize(
    "command",
    [
        # Routed for the interpreter, but naming no ansible CLI: the fixup would only add
        # text for the classifier to read.
        "pytest ansible/tests",
        "./scripts/diagnostics/probe.py health jellyfin",
        "uv run pytest",
        "ls -la",
        # Arguments and prose, not programs.
        "grep -rn ansible-playbook scripts",
        "echo 'run ansible-playbook by hand'",
        "which ansible-playbook",
    ],
)
def test_non_ansible_commands_do_not_carry_the_stdio_fixup(command):
    out = rewrite(command)
    assert out is None or STDIO_FIXUP_FALLBACK not in out, out


def test_the_fixup_is_applied_once():
    """A command already carrying it must not collect a second copy."""
    out = rewrite(STDIO_FIXUP_FALLBACK + "uv run ansible-playbook ansible/deploy.yml")
    assert out is None or out.count("os.set_blocking") == 1


def test_ansible_commands_prefer_stdio_blocking_when_it_is_on_path():
    """When `stdio-blocking` has deployed, the arm prefers it over the inline fallback --
    it does not match the dotfiles repo's `Bash(python3 -c:*)` ask rule, where the
    fallback's inline `python3 -c` does."""
    out = rewrite("ansible-playbook ansible/deploy.yml --check", which=_present)
    assert out is not None
    assert out.startswith(STDIO_FIXUP_STDIO_BLOCKING), out
    assert "os.set_blocking" not in out, out


def test_ansible_commands_fall_back_when_stdio_blocking_is_absent():
    """The other half of the same pair: no `stdio-blocking` on PATH means the inline
    fixup runs instead, so a machine the dotfiles have not deployed to yet still works."""
    out = rewrite("ansible-playbook ansible/deploy.yml --check", which=_absent)
    assert out is not None
    assert out.startswith(STDIO_FIXUP_FALLBACK), out


def test_the_fixup_restores_blocking_on_both_stdout_and_stderr(tmp_path):
    """The functional half: run the fixup with O_NONBLOCK set, and read the flags back.

    A textual assertion that the arm prepends *something* cannot see whether that
    something works. This reproduces the harness's own shape — a regular file opened
    non-blocking — and asserts both flags are clear by the next command in the same shell.

    stdout and stderr are separate files here on purpose. The Bash tool gives them one
    shared open file description, so a fixup that only ever reached stdout would pass a
    single-file test while leaving the stderr half to luck.
    """
    out, err = tmp_path / "out", tmp_path / "err"
    out_fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK)
    err_fd = os.open(err, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK)
    try:
        assert not os.get_blocking(out_fd) and not os.get_blocking(err_fd)
        probe = "python3 -c 'import os; print(os.get_blocking(1), os.get_blocking(2))'"
        subprocess.run(
            ["bash", "-c", STDIO_FIXUP_FALLBACK + probe],
            stdout=out_fd,
            stderr=err_fd,
            check=True,
            timeout=60,
        )
    finally:
        os.close(out_fd)
        os.close(err_fd)
    assert out.read_text(encoding="utf-8").strip() == "True True"


def test_the_fixup_is_needed_because_a_non_blocking_child_reads_non_blocking(tmp_path):
    """The control: without the fixup the same shell sees O_NONBLOCK on both, so the test
    above is measuring the fixup rather than a default."""
    out, err = tmp_path / "out", tmp_path / "err"
    out_fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK)
    err_fd = os.open(err, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK)
    try:
        subprocess.run(
            [
                "bash",
                "-c",
                "python3 -c 'import os; print(os.get_blocking(1), os.get_blocking(2))'",
            ],
            stdout=out_fd,
            stderr=err_fd,
            check=True,
            timeout=60,
        )
    finally:
        os.close(out_fd)
        os.close(err_fd)
    assert out.read_text(encoding="utf-8").strip() == "False False"


# --- heredocs: a document body is data, not a command sequence -----------------------------
#
# The walk reads a newline as a command separator, and a heredoc body is part of the Bash
# tool's command text. A line of prose starting with a `.py` filename must not be spliced as
# if it were a program — e.g. a commit message reading `uv run health.py had grown to 938
# lines`. Auto mode writes files through heredocs too, so `cat > foo.py <<'EOF'` is the same
# bug on a more common surface.
#
# Body apostrophes are why these vectors need reading carefully: a body containing "doesn't"
# flips the quote state, and the unterminated-quote bail would return None for the wrong
# reason. The vectors below are apostrophe-free so they isolate the heredoc handling.


def test_a_commit_message_naming_a_py_file_is_left_alone():
    """The reported symptom: `git commit -F -` with a message body naming a script."""
    assert (
        rewrite("git commit -F - <<'EOF'\nSubject line\n\nprobe.py runs the gate.\nEOF")
        is None
    )


@pytest.mark.parametrize(
    "command",
    [
        # An unquoted delimiter is still a delimiter.
        "cat <<EOF > /tmp/note\nprobe.py runs the gate.\nEOF",
        # A double-quoted one likewise.
        'cat <<"EOF" > /tmp/note\npytest -q covers it.\nEOF',
        # `<<-` strips leading tabs from the terminator, so the body ends at the tabbed EOF.
        "cat <<-EOF > /tmp/note\npython3 is named here.\n\tEOF",
        # A heredoc writing a Python file: the body is source, never a command.
        "cat > scripts/foo.py <<'EOF'\nimport os\n\npython3 is discussed, not invoked\nEOF",
        # The body ends the command text with no trailing newline.
        "cat <<'EOF'\nansible-playbook deploy.yml is the command\nEOF",
    ],
)
def test_heredoc_bodies_are_never_rewritten(command):
    assert rewrite(command) is None


def test_a_command_after_a_heredoc_is_still_rewritten():
    """The proof the fix skips the body rather than abandoning the whole command.

    Without this, a fix that simply bailed on any heredoc would pass every test above while
    leaving the `pytest` after it bare — the silent failure this arm exists to prevent.
    """
    command = "cat <<'EOF' > /tmp/note\nprobe.py runs the gate.\nEOF\npytest -q"
    assert (
        rewrite(command)
        == "cat <<'EOF' > /tmp/note\nprobe.py runs the gate.\nEOF\nuv run pytest -q"
    )


def test_the_program_before_a_heredoc_is_still_rewritten():
    """The body is skipped; the command introducing it is not."""
    assert (
        rewrite("python3 - <<'EOF'\nprint(1)\nEOF")
        == "uv run python3 - <<'EOF'\nprint(1)\nEOF"
    )


def test_an_unterminated_heredoc_leaves_the_command_alone():
    """Same posture as an unterminated quote: the body's extent is a guess, so do not splice."""
    assert rewrite("cat <<'EOF'\nprobe.py runs the gate.\n") is None


def test_two_heredocs_opened_on_one_line_leave_the_command_alone():
    """Rare enough that bailing beats queueing: the second body's extent is unread."""
    assert rewrite("cat <<A <<B\npytest\nA\npytest\nB") is None


def test_a_here_string_is_not_a_heredoc():
    """`<<<` is a single-line redirection with no body to skip."""
    assert rewrite("pytest <<< 'python3 data'") == "uv run pytest <<< 'python3 data'"


def test_a_body_apostrophe_no_longer_decides_the_outcome():
    """The body is skipped opaquely, so prose quoting cannot corrupt the walk's quote state.

    This vector passed before the fix too — via the unterminated-quote bail, not via any
    understanding of the heredoc. It is kept as the pair to the vectors above, which is why
    those are apostrophe-free.
    """
    command = (
        "git commit -F - <<'EOF'\nSubject\n\nIt doesn't touch probe.py at all.\nEOF"
    )
    assert rewrite(command) is None


# --- through the dispatcher ---------------------------------------------------------------
#
# Everything above drives the arm directly. These two cover the half only the dispatcher can
# get wrong: that the rewrite reaches the emitted `hookSpecificOutput` at all, and that it
# rides beside another arm's verdict rather than replacing it (#3286).


def _dispatch(command, monkeypatch, capsys):
    """`bash-pretool.py`'s parsed output for `command`, or None when it emitted nothing."""
    dispatcher = _load("bash-pretool")
    payload = {
        "tool_name": "Bash",
        "cwd": _REPO,
        "session_id": f"test-{uuid.uuid4()}",
        "tool_input": {"command": command},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert dispatcher.main() == 0
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"] if out.strip() else None


def test_the_dispatcher_emits_the_rewrite(monkeypatch, capsys):
    out = _dispatch("pytest ansible/tests", monkeypatch, capsys)
    assert out["updatedInput"] == {"command": "uv run pytest ansible/tests"}
    assert "permissionDecision" not in out


def test_the_dispatcher_emits_a_rewrite_beside_another_arms_verdict(
    monkeypatch, capsys
):
    """The near miss: a command that both needs routing and trips a deny guard.

    The harness drops an `updatedInput` that arrives under a `deny`, so emitting both is
    equivalent to what the two separate hooks produced — and the verdict must still be there.
    """
    out = _dispatch(
        "kubectl rollout restart deploy/x; pytest",
        monkeypatch,
        capsys,
    )
    assert out["permissionDecision"] == "deny"
    assert out["updatedInput"] == {
        "command": "kubectl rollout restart deploy/x; uv run pytest"
    }
