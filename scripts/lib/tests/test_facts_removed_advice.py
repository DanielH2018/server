"""The removed-name advice: a live doc or a comment naming an identifier the branch removed.

These two classes block nothing. `repo_guards.py` prints them through `vanished_advice`, so
each fixture drives that path. `test_facts_lint_vanished.py` holds the class that blocks.
"""

import pytest

from lib.facts.lint import changed_units, lint_sections, vanished_advice
from lib.facts.removed import comment_name
from lib.git_testing import commit, git, init_repo

_DEFAULTS = "kuma/defaults/main.yml"
_RUNBOOK = "docs/kuma.md"
_TASKS = "kuma/tasks/main.yml"
_OLD = "kuma_maintenance_window_timezone: Atlantic/Reykjavik\nkuma_port: 3001\n"
_NEW = "kuma_maint_tz: Atlantic/Reykjavik\nkuma_port: 3001\n"
_SENTENCE = "## Window\nThe window runs in `kuma_maintenance_window_timezone`.\n"
_COMMENT = "# Reads kuma_maintenance_window_timezone for the window.\nport: 1\n"
_NAME = "  kuma_maintenance_window_timezone: "


def _renamed(tmp_path, **base_files):
    """A repo whose `base` branch holds the old name, with the rename staged on top of it."""
    repo = init_repo(tmp_path)
    commit(repo, "base", **{_DEFAULTS: _OLD, **base_files})
    git(repo, "branch", "base")
    (repo / _DEFAULTS).write_text(_NEW)
    git(repo, "add", "-A")
    return repo


def _advised(repo):
    """The advice's name lines, without its count line."""
    return (vanished_advice(repo, "base") or "").splitlines()[1:]


def _blocking(repo):
    return lint_sections(repo, changed_units(repo, "base"), since="base")


def test_a_live_doc_naming_a_removed_variable_is_advised_and_blocks_nothing(tmp_path):
    repo = _renamed(tmp_path, **{_RUNBOOK: _SENTENCE})
    assert _advised(repo) == [_NAME + "docs/kuma.md#Window"]
    assert _blocking(repo) == []


@pytest.mark.parametrize("path", ["docs/archive/kuma.md", "docs/reference/kuma.md"])
def test_a_doc_about_the_past_or_a_generated_one_is_clean(tmp_path, path):
    repo = _renamed(tmp_path, **{path: _SENTENCE})
    assert vanished_advice(repo, "base") == ""


def test_a_comment_naming_a_variable_the_branch_removed_is_advised(tmp_path):
    repo = _renamed(tmp_path, **{_TASKS: _COMMENT})
    assert _advised(repo) == [_NAME + "kuma/tasks/main.yml:1"]


@pytest.mark.parametrize(
    ("old", "tasks"),
    [
        (_OLD, _COMMENT + "x: '{{ kuma_maintenance_window_timezone_v2 }}'\n"),
        (_OLD + "note: pre-migration\n", "# The pre-migration window.\nport: 1\n"),
    ],
    ids=["held-inside-a-longer-name", "hyphenated-english"],
)
def test_a_comment_whose_name_survives_or_is_no_name_is_clean(tmp_path, old, tasks):
    repo = init_repo(tmp_path)
    commit(repo, "base", **{_DEFAULTS: old, _TASKS: tasks})
    git(repo, "branch", "base")
    (repo / _DEFAULTS).write_text(_NEW)
    assert vanished_advice(repo, "base") == ""


def test_a_name_removed_from_a_comment_alone_is_clean(tmp_path):
    """The other comments that hold the name were stale before this branch."""
    repo = init_repo(tmp_path)
    other = "# Also kuma_retired_key_name.\nport: 2\n"
    commit(
        repo,
        "base",
        **{_TASKS: "# See kuma_retired_key_name.\nport: 1\n", "kuma/o.yml": other},
    )
    git(repo, "branch", "base")
    (repo / _TASKS).write_text("port: 1\n")
    assert vanished_advice(repo, "base") == ""


def test_the_advice_counts_names_and_lists_every_place_on_one_line(tmp_path):
    repo = _renamed(tmp_path, **{_RUNBOOK: _SENTENCE, "kuma/CLAUDE.md": _SENTENCE})
    count, *names = (vanished_advice(repo, "base") or "").splitlines()
    assert count.startswith("removed names: 1 still used.")
    assert names == [_NAME + "docs/kuma.md#Window, kuma/CLAUDE.md#Window"]
    assert vanished_advice(repo, "no-such-ref") is None


@pytest.mark.parametrize(
    ("tok", "expected"),
    [
        ("check_cluster_targets", True),
        ("agent_ssh_login.yml", True),
        ("manifest-prune-check", True),
        ("KVM/libvirt", False),
    ],
)
def test_comment_name(tok, expected):
    assert comment_name(tok) is expected
