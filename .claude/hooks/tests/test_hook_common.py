#!/usr/bin/env python3
"""Tests for `_hook_common.split_stages` — the stage splitter every Bash guard shares.

`shlex.split` never returns a bare `;` token: it leaves the separator glued to the word
before it (`"hi;"`), so a rule keyed on a stage's first word never sees anything after a
`;`. Issue #1020: every `block-footguns.py` and `nudge-land-sh.py` rule was reachable by
writing `;` instead of `&&`, confirmed live — `git stash && ... ; git stash pop` was
ALLOWED and applied another session's 25-file work-in-progress into this tree.

Since #2134 the splitter is the dotfiles package's segmenter, so the `;` cases below pin
what that package must keep doing for this repo, and the newline and heredoc cases pin what
the hand-rolled splitter never did. The module runs under `segmenter_or_skip` (conftest.py):
the deployed package is the thing under test, and CI does not have it.

Every case below is an accept/reject pair: a `;`-joined command that must still split into
stages a rule can see, and a quoted `;` that must NOT split. Run:
    uv run pytest .claude/hooks/tests/test_hook_common.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _hook_common import Unsplittable, split_stages

pytestmark = pytest.mark.usefixtures("segmenter_or_skip")


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
    """Until #2134 this returned `[]`, which every consumer read as "nothing to judge"."""
    with pytest.raises(Unsplittable) as caught:
        split_stages("echo 'unterminated")
    assert caught.value.status == "unreadable:unbalanced-quote"
    assert not caught.value.missing


def test_an_unclosed_substitution_is_refused():
    with pytest.raises(Unsplittable) as caught:
        split_stages("echo $(ls")
    assert caught.value.status == "unreadable:substitution"


@pytest.mark.without_segmenter
def test_a_missing_segmenter_is_refused_as_missing():
    """The half-deployed host: hook code present, `claude_guard` not yet applied. The
    consumer decides what to do with it; the splitter's job is to say which cause it was."""
    with pytest.raises(Unsplittable) as caught:
        split_stages("git stash pop", parse=None)
    assert caught.value.missing
    assert caught.value.status == "segmenter-missing"
