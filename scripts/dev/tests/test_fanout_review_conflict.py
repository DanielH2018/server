"""The review pipeline's one resume for a landing that ends `merge-conflict`.

The fake runner answers `git merge-tree` with `run.conflicts`, and each landing's log ends on
the next of `run.verdicts`.

Run: uv run pytest scripts/dev/tests/test_fanout_review_conflict.py
"""

import json

import pytest

from _review_fakes import PR, _finding, _pipeline, _report
from dev.fanout_lib import review_unit
from dev.fanout_lib.launch import RESUME_FLAG, RESUME_REQUEST, REVIEW_RUNTIME_MAX_S
from dev.fanout_lib.review.review_land import LAND_MARGIN_S

CONFLICT = (
    "VERDICT: merge-conflict (PR #4000 conflicts with master — rebase it, push, "
    "then re-run this)"
)
FILES = ["docs/airflow-warehouse-plan.md", "scripts/x.py"]
NAMED = "Conflicting files: docs/airflow-warehouse-plan.md, scripts/x.py."
CLEAN = _report(structured={"summary": "", "findings": []})
LANDABLE = {
    "headRefName": "worktree-fanout-1345",
    "baseRefName": "master",
    "isCrossRepository": False,
    "author": {"login": "DanielH2018"},
    "files": [{"path": "docs/landing.md"}],
    "body": "",
}


def _conflicted(tmp_path, verdicts, resolution_review=CLEAN, **kwargs):
    """A batch with a clean review whose landings end on `verdicts`, then settle."""
    reports = [
        _report(f"Opened {PR}"),
        CLEAN,
        _report(f"Rebased onto master. {PR}"),
        resolution_review,
    ]
    pipeline, run = _pipeline(tmp_path, reports, **kwargs)
    run.verdicts, run.conflicts = list(verdicts), FILES
    # cc-wait exits 1 for a landing that failed, and 0 once one settles.
    run.waits = [1] * len(verdicts) + [0]
    return pipeline, run


def _phases(run):
    return [phase for _, _, phase in run.claude]


def _landings(run):
    return [c for c in run.lands if c[0].endswith("land.sh")]


def test_a_merge_conflict_resumes_the_implementer_once_and_lands_the_reviewed_resolution(
    tmp_path,
):
    pipeline, run = _conflicted(tmp_path, [CONFLICT])
    final = pipeline.run_all()
    assert final["result"] == f"VERDICT: settled (deployed)\n{PR}"
    assert _phases(run) == ["implement", "review", "conflict", "review"]
    argv, prompt, _ = run.claude[2]
    assert argv[-2:] == ["--resume", "sid-1"]
    assert CONFLICT in prompt and all(f"`{name}`" in prompt for name in FILES)
    assert "git rebase origin/master" in prompt and "--force-with-lease" in prompt
    # The reviewer is pointed at what the rebase changed between the reviewed head and the new one.
    assert "git range-diff base0..aaa base0..bbb" in run.claude[3][1]
    assert len(_landings(run)) == 2
    # The files are read against the base as fetched after the landing, one path per line.
    merge_tree = ["merge-tree", "--write-tree", "--name-only", "--no-messages"]
    asked = run.git.index([*merge_tree, "origin/master", "HEAD"])
    assert ["fetch", "--quiet", "origin", "master"] in run.git[asked - 1 : asked]


def test_a_daniel_server_resolution_the_lander_rule_holds_is_not_landed_again(
    tmp_path,
):
    pipeline, run = _conflicted(
        tmp_path, [CONFLICT], host="daniel-server", heads=("bbb", "ddd")
    )
    run.draft, run.pr_fields = True, LANDABLE
    run.pr_fields_fixed = {
        **LANDABLE,
        "files": [{"path": "scripts/deploy_tools/land.sh"}],
    }
    final = pipeline.run_all()
    assert final["result"].startswith(
        f"needs input: the conflict resolution of {PR} passed its review at head ddd, "
        "and the pipeline did not land it: it changes the lander's own surface"
    )
    assert _phases(run).count("conflict") == 1 and len(_landings(run)) == 1


def test_a_second_conflict_ends_needs_input_naming_the_files(tmp_path):
    pipeline, run = _conflicted(tmp_path, [CONFLICT, CONFLICT])
    final = pipeline.run_all()
    assert final["result"].startswith(
        f"needs input: the landing of {PR} stopped short on a merge conflict, again "
        f"after the one resume for it. {NAMED}\n"
    )
    assert _phases(run).count("conflict") == 1 and len(_landings(run)) == 2


def test_a_conflict_with_too_little_run_time_left_resumes_nobody(tmp_path):
    now = [0.0]
    pipeline, run = _conflicted(tmp_path, [CONFLICT], clock=lambda: now[0])
    inner = pipeline.run

    def landing_spends_the_margin(argv, stdin):
        if "cc-wait" in argv:
            now[0] = REVIEW_RUNTIME_MAX_S - LAND_MARGIN_S + 1
        return inner(argv, stdin)

    pipeline.run = landing_spends_the_margin
    final = pipeline.run_all()
    assert final["result"].startswith(
        f"needs input: the landing of {PR} stopped short on a merge conflict, with too "
        f"little of the unit's run time left to resolve it and land again. {NAMED}\n"
    )
    assert _phases(run) == ["implement", "review"] and len(_landings(run)) == 1


@pytest.mark.parametrize(
    ("resolution_review", "starts"),
    [
        (
            _report(
                structured={
                    "summary": "",
                    "findings": [_finding("The rebase dropped master's retry")],
                }
            ),
            "needs input: the review left a confident medium-or-worse finding after the "
            "conflict resolution, so the PR was not landed: The rebase dropped",
        ),
        (
            _report(is_error=True),
            "needs input: the review of the conflict resolution did not complete",
        ),
    ],
    ids=["a-confident-finding", "a-failed-review"],
)
def test_a_resolution_no_reviewer_passed_is_not_landed(
    tmp_path, resolution_review, starts
):
    pipeline, run = _conflicted(tmp_path, [CONFLICT], resolution_review)
    final = pipeline.run_all()
    assert final["result"].startswith(starts), final["result"]
    assert len(_landings(run)) == 1


@pytest.mark.parametrize(
    ("heads", "pushed"),
    [(("aaa",), True), (("aaa", "zzz"), False)],
    ids=["no-new-commit", "a-commit-left-unpushed"],
)
def test_a_resume_that_pushes_no_new_head_is_not_reviewed_or_landed_again(
    tmp_path, heads, pushed
):
    pipeline, run = _conflicted(tmp_path, [CONFLICT], heads=heads)
    run.resolution_pushed = pushed
    final = pipeline.run_all()
    assert final["result"].startswith(
        f"needs input: the landing of {PR} stopped short on a merge conflict, and the "
        f"one resume for it pushed no new head. {NAMED}\n"
    )
    assert _phases(run) == ["implement", "review", "conflict"]
    assert len(_landings(run)) == 1


def _ended(tmp_path, **kwargs):
    """A batch whose unit has ended, with its first session on disk and a resolution to come."""
    reports = [_report(f"Rebased onto master. {PR}"), CLEAN]
    pipeline, run = _pipeline(tmp_path, reports, **kwargs)
    first = _report(f"Opened {PR}", session="sid-9")
    (tmp_path / ".fanout" / "implement.json").write_text(json.dumps(first))
    run.conflicts = FILES
    request = {"pr": PR, "line": CONFLICT}
    (tmp_path / RESUME_REQUEST).write_text(json.dumps(request))
    return pipeline, run


def test_a_resumed_unit_resumes_the_first_session_for_the_conflict_place_resume_named(
    tmp_path,
):
    pipeline, run = _ended(tmp_path)
    final = review_unit.run(pipeline, [RESUME_FLAG])
    assert final["result"] == f"VERDICT: settled (deployed)\n{PR}"
    assert _phases(run) == ["conflict", "review"]
    argv, prompt, _ = run.claude[0]
    assert argv[-2:] == ["--resume", "sid-9"]
    assert CONFLICT in prompt and "- `scripts/x.py`" in prompt
    assert len(_landings(run)) == 1
    assert pipeline.record.resumed


def test_a_resumed_pr_the_lander_rule_holds_ends_reviewed_and_unlanded(tmp_path):
    pipeline, run = _ended(tmp_path, host="daniel-server")
    run.pr_fields = {**LANDABLE, "files": [{"path": "scripts/deploy_tools/land.sh"}]}
    final = review_unit.run(pipeline, [RESUME_FLAG])
    assert final["result"].startswith(
        f"needs input: the conflict resolution of {PR} passed its review at head bbb, "
        "and the pipeline did not land it: it changes the lander's own surface"
    )
    assert final["result"].endswith(
        "Land it with `land.sh --pr 4000 --arm-merge --await-merge --detach && "
        f"cc-wait land 4000`.\n{PR}"
    )
    assert _phases(run) == ["conflict", "review"] and not _landings(run)
