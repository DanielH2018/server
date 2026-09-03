#!/usr/bin/env python3
"""Tests for `_hook_common.split_stages` — the stage splitter every Bash guard shares.

`shlex.split` never returns a bare `;` token: it leaves the separator glued to the word
before it (`"hi;"`), so a rule keyed on a stage's first word never sees anything after a
`;`. Issue #1020: every `block-footguns.py` and `nudge-land-sh.py` rule was reachable by
writing `;` instead of `&&`, confirmed live — `git stash && ... ; git stash pop` was
ALLOWED and applied another session's 25-file work-in-progress into this tree.

Every case below is an accept/reject pair: a `;`-joined command that must still split into
stages a rule can see, and a quoted `;` that must NOT split. Run:
    uv run pytest .claude/hooks/tests/test_hook_common.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _hook_common import split_stages


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


def test_unbalanced_quotes_yield_no_stages():
    assert split_stages("echo 'unterminated") == []


def test_a_plain_command_is_one_stage():
    assert split_stages("git stash pop") == [["git", "stash", "pop"]]
