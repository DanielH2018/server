"""The staging-gate key's verification procedure, and the newline that made it necessary.

TWO DEFECTS ON 2026-08-29, one hiding the other.

The deployed private key was one byte short. `content: "{{ staging_gate_ssh_key }}"` looks
right, but Ansible strips exactly one trailing newline when templating a variable into a string
field, and OpenSSH rejects a key whose final line is unterminated with `error in libcrypto` —
which reads as corruption rather than as a missing terminator. The SOPS value was a correct
399-byte literal block the whole time.

The second defect is why that one was misdiagnosed. The runbook's negative check was
`ssh -i <key> -o IdentitiesOnly=yes <host> "bash -s"`, and it printed **0** — the signal of the
restriction failing — when the truth was that the key never loaded and ssh fell back to a
default identity. `IdentitiesOnly=yes` does not exclude the default identity files. A check that
reports a security failure when a key merely fails to load is worse than no check.

So the verdict function below must separate "something else authenticated" from "this key
authenticated and was not confined", and the tests that matter are the ones proving it does.
"""

from __future__ import annotations

import subprocess

import yaml
from _helpers import REPO

_REPO = REPO
_SCRIPT = _REPO / "scripts" / "deploy_tools" / "verify_staging_gate_key.sh"
_GITOPS_TASKS = (
    _REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "tasks" / "main.yml"
)

_MARKER = "staging-gate: refused: unknown operation"

# Exit codes, mirrored from the script. Named here so a drift in either direction is a failure
# rather than a silently different meaning.
_OK = 0
_KEY_UNUSABLE = 10
_FELL_BACK = 11
_RESTRICTION_OPEN = 12
_NO_VERDICT = 13


def classify_negative(rc: int, stderr: str) -> int:
    """Drive the script's own verdict function by sourcing it — no network involved."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; classify_negative "$2" "$3"',
            "_",
            str(_SCRIPT),
            str(rc),
            stderr,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode


# ── the one case that means the restriction works ───────────────────────────────────────────


def test_a_refusal_from_the_dispatcher_is_the_pass_case():
    assert classify_negative(71, _MARKER) == _OK


# ── the cases that must NOT read as a pass ──────────────────────────────────────────────────


def test_a_successful_shell_is_the_restriction_being_open():
    """`bash -s` returning 0 means this key is not confined. The loudest possible failure."""
    assert classify_negative(0, "") == _RESTRICTION_OPEN


def test_a_nonzero_exit_without_the_marker_is_a_fallback_not_a_pass():
    """THE 2026-08-29 TRAP, inverted.

    Any old command can exit non-zero. Only the dispatcher prints its refusal marker, so without
    it there is no evidence our key reached our forced command — and calling that a pass is how
    a broken restriction would read as a working one.
    """
    assert classify_negative(1, "bash: line 1: nope: command not found") == _FELL_BACK


def test_the_marker_is_required_even_on_the_expected_exit_code():
    """71 alone is not proof: another program could exit 71 for its own reasons."""
    assert classify_negative(71, "") == _FELL_BACK


def test_the_dispatcher_answering_with_the_wrong_code_is_a_fallback():
    assert classify_negative(3, _MARKER) == _FELL_BACK


def test_ssh_failing_is_no_verdict_rather_than_a_security_finding():
    """255 is ssh itself. It says nothing about the restriction, so it must not read as one."""
    assert (
        classify_negative(255, "ssh: connect to host ... Connection refused")
        == _NO_VERDICT
    )


def test_the_failure_modes_are_all_distinct():
    """A shared exit code would collapse exactly the distinctions this script exists to make."""
    codes = {_OK, _KEY_UNUSABLE, _FELL_BACK, _RESTRICTION_OPEN, _NO_VERDICT}
    assert len(codes) == 5


# ── the precondition, which is what stops a load failure reading as a security failure ──────


def test_an_unloadable_key_is_rejected_before_any_connection(tmp_path):
    """A key one byte short of its trailing newline must be KEY_UNUSABLE, never a shell result.

    Drives the real precondition against a real malformed key, so this covers the actual
    2026-08-29 artefact rather than a description of it.
    """
    good = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(good)],
        capture_output=True,
        check=True,
    )
    truncated = tmp_path / "truncated"
    truncated.write_bytes(good.read_bytes()[:-1])
    truncated.chmod(0o600)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; KEY="$2"; assert_key_is_the_authorized_one',
            "_",
            str(_SCRIPT),
            str(truncated),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == _KEY_UNUSABLE, result.stderr
    assert "does not load" in result.stderr


def test_a_wellformed_key_that_is_not_the_authorized_one_is_rejected(tmp_path):
    """Loading is not enough — it must be the key the role actually authorized."""
    other = tmp_path / "other"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(other)],
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; KEY="$2"; assert_key_is_the_authorized_one',
            "_",
            str(_SCRIPT),
            str(other),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == _KEY_UNUSABLE, result.stderr
    assert "not the key" in result.stderr


# ── the newline itself ──────────────────────────────────────────────────────────────────────


def test_the_key_is_written_with_a_deterministic_trailing_newline():
    """`{{ var }}` alone loses the final newline and OpenSSH then refuses the key.

    Pins both halves: `trim` collapses whatever the stored scalar carries, and the explicit
    newline puts back exactly one. Either alone is wrong.
    """
    tasks = yaml.safe_load(_GITOPS_TASKS.read_text())
    contents = [
        task["ansible.builtin.copy"]["content"]
        for task in tasks
        if isinstance(task, dict)
        and isinstance(task.get("ansible.builtin.copy"), dict)
        and "staging_gate_ssh_key"
        in str(task["ansible.builtin.copy"].get("content", ""))
    ]
    assert contents, "no task writes staging_gate_ssh_key any more"
    for content in contents:
        assert content.endswith("\n"), (
            f"{content!r} does not end in an explicit newline. Ansible strips one when "
            f"templating a variable into a string, so the key lands a byte short and OpenSSH "
            f"refuses it with `error in libcrypto`."
        )
        assert "trim" in content, (
            f"{content!r} does not trim before appending, so a stored value that keeps its "
            f"newline would get two and the key would again not be byte-exact."
        )
