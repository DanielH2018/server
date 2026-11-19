#!/usr/bin/env python3
"""Tests for `_hook_common.split_stages` — the stage splitter every Bash guard shares.

`shlex.split` never returns a bare `;` token: it leaves the separator glued to the word
before it (`"hi;"`), so a rule keyed on a stage's first word never sees anything after a
`;`. Without a `;` split, every `block-footguns.py` rule is reachable by writing `;` instead of `&&`.

The splitter is the dotfiles package's segmenter, so the `;` cases below pin what that
package must keep doing for this repo, and the newline and heredoc cases pin what a
hand-rolled splitter cannot do. The deployed package is the thing under test; CI links a
pinned dotfiles checkout into the deployed path, so these run there too.

Every case below is an accept/reject pair: a `;`-joined command that must still split into
stages a rule can see, and a quoted `;` that must NOT split. Run:
    uv run pytest .claude/hooks/tests/test_hook_common.py
"""

import io
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _hook_common import (
    Unsplittable,
    emit_pretooluse_decision,
    gh_repo,
    parse_payload,
    primary_checkout,
    read_payload,
    session_state_path,
    split_stages,
)
from lib.git_testing import git, init_repo


def test_the_deployed_segmenter_is_present():
    """Every case below splits through the deployed package, so a missing one fails here by name.

    Without this, a runner that lost the dotfiles checkout would fail the rule tests one by one,
    each reading as a rule bug.
    """
    import _hook_common

    assert _hook_common._parse is not None, (
        "claude_guard is not deployed: run chezmoi apply here, or check the dotfiles "
        "checkout step of the pytest job in .github/workflows/ci.yml"
    )


# --- the bug: `;` must split like `&&` does -----------------------------------------------


def test_semicolon_splits_into_two_stages():
    """The exact shape from the issue: `;` must yield a stage starting with `kubectl`."""
    assert split_stages("echo hi; kubectl rollout restart deploy/x") == [
        ["echo", "hi"],
        ["kubectl", "rollout", "restart", "deploy/x"],
    ]


def test_semicolon_with_no_surrounding_space_still_splits():
    assert split_stages("a;b") == [["a"], ["b"]]


def test_the_live_incident_shape_splits_into_three_stages():
    """`git stash && ... ; git stash pop` — the command that actually got through."""
    assert split_stages(
        "git stash && prek run --all-files vale 2>&1 | tail -5; git stash pop"
    ) == [
        ["git", "stash"],
        ["prek", "run", "--all-files", "vale", "2>&1"],
        ["tail", "-5"],
        ["git", "stash", "pop"],
    ]


# --- the near miss: a quoted `;` must NOT split -----------------------------------------


def test_a_semicolon_inside_single_quotes_does_not_split():
    assert split_stages("echo 'a; b'") == [["echo", "a; b"]]


def test_a_semicolon_inside_double_quotes_does_not_split():
    assert split_stages('echo "a; b"') == [["echo", "a; b"]]


def test_a_backslash_escaped_semicolon_does_not_split():
    assert split_stages("echo hi\\; there") == [["echo", "hi;", "there"]]


# --- the case-statement terminators named in the issue ----------------------------------


def test_double_semicolon_collapses_to_one_boundary():
    """`;;` must not leave a stray leading `;` glued to the next stage."""
    assert split_stages("cmd1;;cmd2") == [["cmd1"], ["cmd2"]]


def test_semicolon_ampersand_collapses_to_one_boundary():
    """`;&` must not leave a stray leading `&` glued to the next stage."""
    assert split_stages("cmd1;&cmd2") == [["cmd1"], ["cmd2"]]


# --- existing behavior must survive ------------------------------------------------------


def test_double_ampersand_still_splits():
    assert split_stages("git fetch && gh run watch") == [
        ["git", "fetch"],
        ["gh", "run", "watch"],
    ]


def test_pipe_still_splits():
    assert split_stages("grep -rl foo . | xargs sed -i") == [
        ["grep", "-rl", "foo", "."],
        ["xargs", "sed", "-i"],
    ]


def test_a_plain_command_is_one_stage():
    assert split_stages("git stash pop") == [["git", "stash", "pop"]]


# --- what the hand-rolled splitter never did ---------------------------------------------


def test_a_newline_splits_like_a_semicolon():
    """`shlex.split` read a newline as whitespace, so `git fetch\ngh run watch` was ONE stage
    whose first word was `git` — and no rule keyed on `gh` ever saw it."""
    assert split_stages("git fetch\ngh run watch") == [
        ["git", "fetch"],
        ["gh", "run", "watch"],
    ]


def test_a_heredoc_body_is_not_a_stage():
    """The body is data the interpreter reads, not commands the shell runs."""
    assert split_stages("python3 - <<'EOF'\ngrep -Z x\nEOF\nls") == [
        ["python3", "-", "<<EOF"],
        ["ls"],
    ]


# --- a command that cannot be read is a refusal, never an empty answer -------------------


def test_unbalanced_quotes_are_refused():
    """A non-ok parse is not `[]`: every consumer would read that as "nothing to judge"."""
    with pytest.raises(Unsplittable) as caught:
        split_stages("echo 'unterminated")
    assert caught.value.status == "unreadable:unbalanced-quote"
    assert not caught.value.missing


def test_an_unclosed_substitution_is_refused():
    with pytest.raises(Unsplittable) as caught:
        split_stages("echo $(ls")
    assert caught.value.status == "unreadable:substitution"


def test_a_missing_segmenter_is_refused_as_missing():
    """The half-deployed host: hook code present, `claude_guard` not yet applied. The
    consumer decides what to do with it; the splitter's job is to say which cause it was."""
    with pytest.raises(Unsplittable) as caught:
        split_stages("git stash pop", parse=None)
    assert caught.value.missing
    assert caught.value.status == "segmenter-missing"


# --- the helpers the hook entry points share ---------------------------------------------


@pytest.mark.parametrize(
    "words",
    [
        ["gh", "issue", "create", "-R", "o/r"],
        ["gh", "issue", "create", "-Ro/r"],
        ["gh", "issue", "create", "--repo", "o/r"],
        ["gh", "issue", "create", "--repo=o/r"],
    ],
)
def test_gh_repo_reads_every_spelling_of_the_flag(words):
    assert gh_repo(words) == "o/r"


def test_gh_repo_ignores_a_github_url_in_the_body():
    """A URL is text to `gh issue create`; reading it would aim the suggestion elsewhere."""
    words = ["gh", "issue", "create", "--body", "see https://github.com/o/r/issues/1"]
    assert gh_repo(words) is None


def test_a_session_id_with_path_characters_stays_inside_the_temp_dir(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    path = session_state_path("ci-poll", "../../etc/passwd")
    assert os.path.dirname(path) == str(tmp_path)
    assert os.path.basename(path) == "claude-ci-poll-etcpasswd"


@pytest.mark.parametrize("text", ["", "{nope", "[1, 2]", '"a string"'])
def test_a_payload_that_is_not_a_json_object_reads_as_none(text):
    assert read_payload(io.StringIO(text)) is None


def test_a_json_object_payload_is_parsed():
    event = read_payload(io.StringIO('{"session_id": "s", "source": "startup"}'))
    assert event is not None
    assert event.session_id == "s"
    assert event.raw["source"] == "startup"


def test_a_bash_payload_is_a_bash_call_keeping_its_tool_input(tmp_path):
    tool_input = {"command": "ls", "run_in_background": True}
    call = parse_payload(
        {
            "tool_name": "Bash",
            "tool_input": tool_input,
            "cwd": str(tmp_path),
            "agent_type": "skeptic",
            "permission_mode": "auto",
        }
    ).bash()
    assert call is not None
    assert call.command == "ls"
    assert call.cwd == tmp_path
    assert call.agent_type == "skeptic"
    assert call.agent_id is None
    assert call.permission_mode == "auto"
    assert call.tool_input == tool_input


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Bash", "tool_input": None},
        {"tool_name": "Bash", "tool_input": {"command": None}},
        {"tool_name": "Bash", "tool_input": {"command": ""}},
        {"tool_name": "Bash", "tool_input": "ls"},
        {"tool_name": "Edit", "tool_input": {"command": "ls"}},
        {"tool_input": {"command": "ls"}},
    ],
    ids=["null-input", "null-command", "empty", "non-object", "edit", "no-tool"],
)
def test_a_payload_with_no_bash_command_is_no_bash_call(payload):
    assert parse_payload(payload).bash() is None


@pytest.mark.parametrize("cwd", [None, "", "relative/dir", 7], ids=repr)
def test_a_payload_with_no_absolute_cwd_runs_in_the_process_cwd(
    cwd, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    if cwd is not None:
        payload["cwd"] = cwd
    call = parse_payload(payload).bash()
    assert call is not None
    assert call.cwd == tmp_path


def test_an_edit_resolves_a_relative_file_path_against_the_payload_cwd(tmp_path):
    event = parse_payload(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "a/b.md"},
            "cwd": str(tmp_path),
        }
    )
    call = event.edit()
    assert call is not None
    assert call.file_path == tmp_path / "a" / "b.md"


@pytest.mark.parametrize(
    "tool_input", [None, {}, {"file_path": None}, {"file_path": ""}]
)
def test_an_edit_with_no_file_path_is_no_edit_call(tool_input):
    assert (
        parse_payload({"tool_name": "Write", "tool_input": tool_input}).edit() is None
    )


# Two ways to name the primary checkout were in use, and they disagree exactly where the git
# dir has no checkout beside it. The tests below pin which one is right.


def test_primary_checkout_is_the_same_from_the_primary_and_a_linked_worktree(tmp_path):
    primary = init_repo(tmp_path / "primary", initial_commit="init")
    worktree = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", "-b", "wt", str(worktree))

    expected = os.path.realpath(primary)
    assert os.path.realpath(primary_checkout(str(primary)) or "") == expected
    assert os.path.realpath(primary_checkout(str(worktree)) or "") == expected


def test_primary_checkout_is_none_for_a_separated_git_dir(tmp_path):
    # `git worktree list --porcelain` names the git dir itself as the main worktree here, so
    # a dirty check run on its first entry would read a directory that is not a checkout.
    checkout = tmp_path / "checkout"
    git_dir = tmp_path / "separate"
    git(tmp_path, "init", "-q", f"--separate-git-dir={git_dir}", str(checkout))
    listed = git(checkout, "worktree", "list", "--porcelain").stdout.splitlines()[0]
    assert listed == f"worktree {git_dir}"
    assert primary_checkout(str(checkout)) is None


def test_primary_checkout_is_none_for_a_bare_repo_worktree(tmp_path):
    bare = init_repo(tmp_path / "bare", bare=True)
    seed = init_repo(tmp_path / "seed", initial_commit="init")
    git(seed, "push", "-q", str(bare), "master")
    worktree = tmp_path / "wt"
    git(bare, "worktree", "add", "-q", str(worktree), "master")
    assert primary_checkout(str(worktree)) is None


def test_primary_checkout_is_none_outside_git(tmp_path):
    assert primary_checkout(str(tmp_path)) is None


def test_a_decision_reason_opens_with_the_deciding_hook(capsys):
    emit_pretooluse_decision("block-protected-edits", "deny", "no")
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecisionReason"] == "[block-protected-edits] no"
