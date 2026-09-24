#!/usr/bin/env python3
"""`scripts/deploy.sh` asks whether the tree is stale BEFORE it validates --tags (issue #1566).

Both refusals mean nothing was deployed, but they name different causes and consumers act on
them differently: `land.sh` retries exit 4 (stale tree) after the tick fast-forwards, and
reports exit 2 (tag miss) as `deploy-failed cause=tag-miss` -- "your change broke something".

`deploy_tags.py validate` reads the CHECKOUT's own containers_list, so on a tree that is behind
origin/master it answers about the wrong tree. The first landing of a new k8s role therefore
read as a tag miss whenever the tick had not yet fast-forwarded the merge commit, because
PR #1555 derives `land.sh`'s tags from the merge commit while deploy.sh validated them against
the primary checkout. Ordering staleness first makes the stale tree the reported cause.

Both halves, per CLAUDE.md: a stale tree carrying an unknown tag must refuse as STALE (the
half the bug got wrong), and an unknown tag on a current tree must still refuse as a TAG MISS
(the half a naive reorder could delete by never reaching the tag check at all).

The gates run in process in `deploy_run.py` (#2412), so `run_front_half` injects their
verdicts; the order they are asked in is the real code.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_staleness_precedes_tag_validation.py
"""

from _deploy_sh_fakes import make_snapshot_repo, run_front_half

_STALE_EXIT = 4
_TAG_MISS_EXIT = 2


def _run(
    tmp_path, monkeypatch, *, stale, validate, tag="definitely-not-a-real-service"
):
    repo = make_snapshot_repo(tmp_path / "repo")
    argv = ["--tags", tag] if tag else []
    code, calls = run_front_half(
        monkeypatch, repo, argv, stale=stale, validate=validate
    )
    return code, [c[0] for c in calls], calls


def test_a_stale_tree_with_an_unknown_tag_refuses_as_stale(tmp_path, monkeypatch):
    """RED half: the shape issue #1566 reported -- a new role's tag on a not-yet-pulled tree."""
    code, names, _ = _run(tmp_path, monkeypatch, stale=1, validate=1)
    assert code == _STALE_EXIT
    # The tag was never judged against the wrong tree, and nothing was deployed.
    assert "validate" not in names and "exec" not in names, names


def test_an_unknown_tag_on_a_current_tree_is_still_a_tag_miss(tmp_path, monkeypatch):
    """CLEAN half: reordering must not swallow the tag check it moved past."""
    code, names, _ = _run(tmp_path, monkeypatch, stale=0, validate=1)
    assert code == _TAG_MISS_EXIT
    assert "validate" in names and "exec" not in names, names


def test_the_staleness_gate_is_told_which_tags_are_being_deployed(
    tmp_path, monkeypatch
):
    """The gate refuses only on a commit reaching what this run renders, so it needs the tags.

    Both halves: a tagged run hands them over, and an untagged run (the wrapper's own
    --changed path reaches the gate before deriving any) asks the unscoped question.
    """
    tagged_dir, untagged_dir = tmp_path / "tagged", tmp_path / "untagged"
    _, _, tagged = _run(tagged_dir, monkeypatch, stale=0, validate=0, tag="uptime-kuma")
    assert next(c for c in tagged if c[0] == "staleness")[1] == ("uptime-kuma",)
    _, _, untagged = _run(untagged_dir, monkeypatch, stale=0, validate=0, tag=None)
    assert next(c for c in untagged if c[0] == "staleness")[1] == ()


def test_the_staleness_question_is_asked_before_the_tag_question(tmp_path, monkeypatch):
    """Both gates pass: the order they were asked in is what this pins.

    Asserted on the call log rather than on an exit code, so the ordering stays checked even
    once neither gate refuses -- an exit-code-only test agrees with an implementation that
    happens to answer 4 for another reason.
    """
    code, names, _ = _run(tmp_path, monkeypatch, stale=0, validate=0, tag="uptime-kuma")
    assert code is None, names
    assert names.index("staleness") < names.index("validate") < names.index("exec")


def test_a_helper_that_exits_or_crashes_returns_a_status_instead_of_ending_the_run():
    """In process, a helper's `sys.exit(2)` would otherwise end the WRAPPER with exit 2 -- a
    tag miss to every consumer -- where the subprocess it replaced reported 2 as its status."""
    import sys

    import deploy_run

    assert deploy_run._call(sys.exit, 2) == 2
    assert deploy_run._call(lambda: 1 / 0) == 1
    assert deploy_run._call(lambda: 0) == 0
