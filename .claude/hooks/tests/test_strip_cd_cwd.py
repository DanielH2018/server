"""Tests for the strip-cd-cwd PreToolUse rewrite arm.

Every shape is an accept/reject pair: a strip that silently stopped matching leaves every
command as typed, which looks exactly like a command that never had a `cd` to drop. The one
reject that matters most is the primary checkout seen from a worktree, because the hook's own
process runs in the primary checkout and a comparison against it would move the command there.

Run: uv run pytest .claude/hooks/tests/test_strip_cd_cwd.py
"""

import importlib.util
import os

_HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_spec = importlib.util.spec_from_file_location(
    "strip_cd_cwd", os.path.join(_HOOKS, "strip-cd-cwd.py")
)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
arm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arm)


def _payload(command, cwd):
    return {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}


def test_a_cd_into_the_cwd_is_stripped(tmp_path):
    assert arm.rewrite(_payload(f"cd {tmp_path} && git status", str(tmp_path))) == (
        "git status"
    )


def test_a_cd_into_another_directory_is_kept(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert arm.rewrite(_payload(f"cd {other} && git status", str(tmp_path))) is None


def test_a_cd_into_the_primary_checkout_from_a_worktree_is_kept(tmp_path, monkeypatch):
    """The hook process runs in the primary checkout; the session's cwd is the worktree."""
    worktree = tmp_path / ".claude" / "worktrees" / "x"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert arm.rewrite(_payload(f"cd {tmp_path} && git status", str(worktree))) is None


def test_quoted_relative_and_trailing_slash_forms_of_the_cwd_are_stripped(tmp_path):
    cwd = str(tmp_path)
    for command in (
        f"cd '{cwd}' && ls",
        f'cd "{cwd}" && ls',
        f"cd {cwd}/ && ls",
        "cd . && ls",
        f"  cd {cwd}&&ls",
        f"cd {cwd} && cd {cwd} && ls",
    ):
        assert arm.rewrite(_payload(command, cwd)) == "ls", command


def test_a_cd_into_a_symlink_to_the_cwd_is_stripped(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    assert arm.rewrite(_payload(f"cd {link} && ls", str(tmp_path))) == "ls"


def test_shapes_the_arm_cannot_read_are_kept(tmp_path):
    cwd = str(tmp_path)
    for command in (
        'cd "$PWD" && ls',
        "cd `pwd` && ls",
        "cd - && ls",
        f"cd -P {cwd} && ls",
        f"cd {cwd}; ls",
        f"cd {cwd} || ls",
        f"cd {cwd} &&",
        f"ls && cd {cwd} && ls",
        f"cd {cwd}",
    ):
        assert arm.rewrite(_payload(command, cwd)) is None, command


def test_a_payload_without_an_absolute_cwd_is_kept(tmp_path):
    assert arm.rewrite(_payload("cd . && ls", "")) is None
    assert arm.rewrite(_payload("cd . && ls", "relative")) is None
