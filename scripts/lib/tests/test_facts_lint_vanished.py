"""`vanished-identifier`: a CLAUDE.md naming an identifier the branch removed from the tree.

Each fixture drives the prek hook's own path, `lint_sections(repo, changed_units(repo, ref),
since=ref)`. The commit that removes an identifier is usually code-only, so `changed_units` is
empty there, and a test that passed `unit_keys=None` instead would pass while the hook never
fired.
"""

import pytest

from lib.facts.lint import changed_units, identifiers, lint_sections
from lib.git_testing import commit, git, init_repo

_DEFAULTS = "kuma/defaults/main.yml"
_DOC = "kuma/CLAUDE.md"
# The shape of a91ed2c6d: a defaults file renames the variable and the doc keeps the old name.
_OLD = "kuma_maintenance_window_timezone: Atlantic/Reykjavik\nkuma_port: 3001\n"
_NEW = "kuma_maint_tz: Atlantic/Reykjavik\nkuma_port: 3001\n"
_SENTENCE = "## Window\nThe window runs in `kuma_maintenance_window_timezone`.\n"


def _branch(tmp_path, doc=_SENTENCE, **base_files):
    """A repo whose `base` branch holds the pre-rename tree, checked out on a branch off it."""
    repo = init_repo(tmp_path)
    commit(repo, "base", **{_DEFAULTS: _OLD, _DOC: doc, **base_files})
    git(repo, "branch", "base")
    return repo


def _stage(repo, **files):
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")


def _vanished(repo):
    keys = changed_units(repo, "base")
    return [
        f
        for f in lint_sections(repo, keys, since="base")
        if f.rule == "vanished-identifier"
    ]


@pytest.mark.parametrize("committed", [False, True], ids=["staged", "committed"])
def test_renamed_variable_still_named_by_a_doc_is_flagged(tmp_path, committed):
    repo = _branch(tmp_path)
    _stage(repo, **{_DEFAULTS: _NEW})
    if committed:
        git(repo, "commit", "-q", "-m", "rename", "--no-gpg-sign")
    assert changed_units(repo, "base") == set()
    found = _vanished(repo)
    assert [(f.unit, f.warn) for f in found] == [("kuma/CLAUDE.md#Window", False)]
    assert found[0].detail.startswith(
        "`kuma_maintenance_window_timezone` was removed in "
    )
    # With no range the rule has nothing to read: a full lint never runs it.
    assert not [f for f in lint_sections(repo, None) if f.rule == "vanished-identifier"]


def test_a_commit_that_also_edits_the_sentence_is_clean(tmp_path):
    repo = _branch(tmp_path)
    _stage(
        repo,
        **{_DEFAULTS: _NEW, _DOC: "## Window\nThe window runs in `kuma_maint_tz`.\n"},
    )
    assert _vanished(repo) == []


def test_a_sentence_marked_as_history_is_clean(tmp_path):
    doc = (
        "## Window\n- **HISTORY — renamed 2026-10-01.** It was\n"
        "  `kuma_maintenance_window_timezone`.\n"
    )
    repo = _branch(tmp_path, doc=doc)
    _stage(repo, **{_DEFAULTS: _NEW})
    assert _vanished(repo) == []


def test_an_identifier_that_survives_elsewhere_is_clean(tmp_path):
    """Removed from one file, still held by another: the doc's claim has not gone stale."""
    repo = _branch(
        tmp_path,
        **{"kuma/tasks/main.yml": "tz: '{{ kuma_maintenance_window_timezone }}'\n"},
    )
    _stage(repo, **{_DEFAULTS: _NEW})
    assert _vanished(repo) == []


def test_a_renovate_tag_bump_is_not_an_error(tmp_path):
    """#4012: a Renovate PR must not arrive red over documentation. Its versions are exempt."""
    doc = "## Image\nThe app runs `org/app:1.2-alpine`.\n"
    repo = _branch(
        tmp_path,
        doc=doc,
        **{"app/defaults/main.yml": "x_image: org/app:1.2-alpine@sha256:a\n"},
    )
    _stage(repo, **{"app/defaults/main.yml": "x_image: org/app:1.3-alpine@sha256:b\n"})
    keys = changed_units(repo, "base")
    assert not [f for f in lint_sections(repo, keys, since="base") if not f.warn]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("`kuma_maintenance_window_timezone`", {"kuma_maintenance_window_timezone"}),
        ("`.claude/rules/facts.md`", {".claude/rules/facts.md"}),
        ("`./scripts/deploy.sh --tags`", {"scripts/deploy.sh"}),
        ("`moby/buildkit:v0.33.0-rootless`", {"moby/buildkit"}),
        ("`2026.10.0-ls253` `10.11.11ubu2604-ls47` `10.0.0.240` `3.14-alpine`", set()),
        ("`deploy` `a_b` `token`", set()),
    ],
)
def test_identifiers(text, expected):
    assert identifiers(text) == expected
