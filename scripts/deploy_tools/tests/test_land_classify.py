"""Resolving the merge commit and classifying what the PR reaches, before any wait.

Run: uv run pytest scripts/deploy_tools/tests/test_land_classify.py
"""

from __future__ import annotations

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes
from deploy_tools.land_lib import classify
from deploy_tools.land_lib.outcome import Outcome


def test_resolve_records_the_merge_sha_and_fetches_master(landing):
    ln, calls = landing()
    classify.resolve(ln)
    assert ln.merge_sha == MERGE_SHA and ln.ledger.merge_sha == MERGE_SHA
    assert ln.ledger.t_merged is not None
    assert ("git", ("fetch", "-q", "origin", "master"), {"cwd": PRIMARY}) in calls


def test_a_pr_with_no_merge_commit_dies(landing):
    ln, _ = landing(Fakes(gh_views={"mergeCommit": {"mergeCommit": None}}))
    with pytest.raises(Outcome) as exc:
        classify.resolve(ln)
    assert exc.value.rc == 1 and "no merge commit" in exc.value.error


def test_the_range_comes_from_the_pull_ref_and_its_merge_base(landing):
    """Not `--since` (other sessions' work) and not `MERGE_SHA^` (wrong for a rebase merge)."""
    ln, calls = landing()
    ln.merge_sha = MERGE_SHA
    assert classify.pr_range(ln) == "prbase..prhead"
    git = [c[1] for c in calls if c[0] == "git"]
    assert ("fetch", "-q", "origin", "refs/pull/999/head") in git
    assert ("merge-base", "prhead", MERGE_SHA) in git


def test_an_unreadable_range_is_empty_and_says_so(landing, capsys):
    """Empty classifies every broad path as loud -- the direction a wrong answer must fall (#848)."""
    ln, _ = landing(Fakes(pull_ref_rc=1))
    assert classify.pr_range(ln) == ""
    assert "every broad path stays owed to a hand" in capsys.readouterr().out


def test_classify_fills_tags_plane_and_self_applied(landing):
    ln, _ = landing(
        Fakes(
            plane="initial_setup.yml --tags k3s",
            self_applied=True,
            derived=(["sonarr", "radarr"], "pr"),
        )
    )
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert (ln.tags, ln.plane, ln.self_applied, ln.needs_diff) == (
        "sonarr,radarr",
        "initial_setup.yml --tags k3s",
        True,
        False,
    )


def test_explicit_tags_skip_derivation(landing):
    ln, calls = landing(tags="sonarr")
    classify.classify(ln)
    assert ln.tags == "sonarr" and not [c for c in calls if c[0].startswith("gh:files")]


def test_a_truncated_file_list_without_since_is_a_usage_error(landing):
    ln, _ = landing(Fakes(derived=([], "fallback")))
    ln.merge_sha = MERGE_SHA
    with pytest.raises(Outcome) as exc:
        classify.classify(ln)
    assert exc.value.rc == 2 and "--since" in exc.value.error


def test_a_truncated_file_list_with_since_defers_derivation(landing, capsys):
    ln, _ = landing(Fakes(derived=([], "fallback")), since="abc")
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.needs_diff and ln.tags == ""
    assert "deriving from the diff since abc after the tick" in capsys.readouterr().out


def test_nothing_to_deploy_when_nothing_is_reached(landing):
    ln, _ = landing()
    with pytest.raises(Outcome) as exc:
        classify.shortcut_if_nothing(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "nothing-to-deploy")


@pytest.mark.parametrize("attr", ["tags", "plane", "self_applied", "needs_diff"])
def test_any_reach_disables_the_shortcut(landing, attr):
    ln, _ = landing()
    setattr(ln, attr, "x" if attr in ("tags", "plane") else True)
    classify.shortcut_if_nothing(ln)
