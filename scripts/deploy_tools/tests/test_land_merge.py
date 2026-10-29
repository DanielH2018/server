"""--arm-merge and --await-merge, driven directly against a fake gh.

The failure the await half guards is a landing that SITS: a PR never merges from
CONFLICTING or from a red PR CI, and GitHub reports neither in `state`, so the loop burned
the 2700s budget and printed merge-timeout. The accept halves matter as much: GitHub
serves `mergeable: UNKNOWN` until it computes mergeability, and await_ci answers `pending`
until a required check registers.

WHICH ASSERTIONS ARE ON TEXT, AND WHY. A printed line is asserted here only where it is the
ONLY thing that distinguishes two behaviours that make the identical `gh` calls. Everywhere the `calls` list already
settles what happened, the wording is not asserted: a text assertion standing in for a
behaviour the fakes already record breaks on a rewording and proves nothing extra.

Run: uv run pytest scripts/deploy_tools/tests/test_land_merge.py
"""

import subprocess
from pathlib import Path

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import merge
from deploy_tools.land_lib.outcome import Outcome

_OPEN = {"state": "OPEN", "title": "Bump vale to 3.19.0", "body": "Closes #12\n"}


def _wait(states: list[str]):
    return [
        {"state": s, "mergeable": m, "headRefOid": h}
        for s, m, h in (x.split() for x in states)
    ]


def test_arm_merge_leaves_the_pr_title_for_the_direct_merge(landing):
    ln, calls = landing(
        Fakes(gh_views={"state,title,body,reviewDecision": _OPEN}),
        arm_merge=True,
        await_merge=True,
    )
    merge.arm_merge(ln)
    assert ln.direct_merge_subject == "Bump vale to 3.19.0"
    assert not [c for c in calls if c[0] == "gh"]


def test_arm_merge_subject_overrides_the_pr_title(landing):
    ln, _ = landing(
        Fakes(gh_views={"state,title,body,reviewDecision": _OPEN}),
        subject="Pin vale",
        await_merge=True,
    )
    merge.arm_merge(ln)
    assert ln.direct_merge_subject == "Pin vale"


def test_arm_merge_is_a_no_op_on_a_merged_pr(landing):
    ln, calls = landing(
        Fakes(
            gh_views={"state,title,body,reviewDecision": {**_OPEN, "state": "MERGED"}}
        )
    )
    merge.arm_merge(ln)
    assert not [c for c in calls if c[0] == "gh"]


def test_arm_merge_dies_on_a_closed_pr(landing):
    ln, _ = landing(
        Fakes(
            gh_views={"state,title,body,reviewDecision": {**_OPEN, "state": "CLOSED"}}
        )
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1 and "closed without merging" in exc.value.error


def _by(login: str) -> dict:
    return {"author": {"is_bot": login.startswith("app/"), "login": login}}


def test_arm_merge_refuses_another_author_when_one_is_required(landing):
    """The renovate agent's contract, enforced: a human's PR is never armed by its session."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title,body,reviewDecision": _OPEN,
                "author": _by("DanielH2018"),
            }
        ),
        arm_merge=True,
        require_author="app/renovate",
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1 and "DanielH2018, not app/renovate" in exc.value.error
    assert not [c for c in calls if c[0] == "gh"]


def test_arm_merge_accepts_the_required_authors_pr(landing):
    ln, _ = landing(
        Fakes(
            gh_views={
                "state,title,body,reviewDecision": _OPEN,
                "author": _by("app/renovate"),
            }
        ),
        arm_merge=True,
        await_merge=True,
        require_author="app/renovate",
    )
    merge.arm_merge(ln)
    assert ln.direct_merge_subject


@pytest.mark.parametrize(
    "body",
    [
        "Filed and not fixed: #2509",  # PR #2510's own wording, which closed #2509
        "This also resolves #17 eventually.",
        "Half of it fixes #17.",
    ],
)
def test_a_closing_keyword_inside_a_sentence_is_stray(body):
    assert merge.stray_closing_refs(body) == [body]


@pytest.mark.parametrize(
    "body",
    [
        "Closes #2513",
        "- Closes #2513\n- Closes #2514",
        "**Fixes #12**",
        "Filed for later: #2509",
        "See #2509 for the follow-up.",
        # The three shapes the corpus of merged PR bodies contains. A rule keyed on the
        # LINE rather than the clause flagged all of them.
        "Closes #2428, closes #2429.",
        "Three test-tree issues from one fan-out batch. Closes #2379. Closes #2404.",
        "**`Closes #2413` — the SSH directory task hardened root's `.ssh`.**",
    ],
)
def test_a_deliberate_close_and_a_bare_reference_are_clean(body):
    assert merge.stray_closing_refs(body) == []


def test_arm_merge_refuses_a_body_that_would_close_an_unfixed_issue(landing):
    """A body saying "not fixed: #N" closes #N on merge, so the arm refuses before merging."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title,body,reviewDecision": {
                    **_OPEN,
                    "body": "Filed and not fixed: #2509\n",
                }
            }
        ),
        arm_merge=True,
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1 and "Filed and not fixed: #2509" in exc.value.error
    # The refusal is worth nothing unless it happens BEFORE the merge call: the damage is
    # done at merge time and reopening the issue is a hand step.
    assert not [c for c in calls if c[0] == "gh"]


@pytest.mark.parametrize("review", ["", "REVIEW_REQUIRED"])
def test_arm_merge_refuses_while_the_repo_is_private(landing, review):
    """No ruleset is enforced on a private free-plan repo, so the direct merge arm_merge
    leaves for await_merge may not go ahead (#3610)."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title,body,reviewDecision": {**_OPEN, "reviewDecision": review}
            },
            repo={"visibility": "private"},
        ),
        arm_merge=True,
        await_merge=True,
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1 and "repo is private" in exc.value.error
    assert not [c for c in calls if c[0] == "gh"]
    assert not ln.direct_merge_subject


def test_arm_merge_accepts_a_public_repo_after_reading_its_visibility(landing):
    ln, calls = landing(
        Fakes(gh_views={"state,title,body,reviewDecision": _OPEN}),
        arm_merge=True,
        await_merge=True,
    )
    merge.arm_merge(ln)
    assert "gh:repo" in [c[0] for c in calls]
    assert ln.direct_merge_subject


def test_arm_merge_names_the_field_when_the_visibility_read_is_the_wrong_shape(landing):
    ln, _ = landing(
        Fakes(gh_views={"state,title,body,reviewDecision": _OPEN}),
        arm_merge=True,
        await_merge=True,
    )
    read = ln.tools.gh_json
    ln.tools.gh_json = lambda *a, **k: (
        ["public"] if a[:2] == ("api", "repos/{owner}/{repo}") else read(*a, **k)
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1
    assert "gh api repos: expected a JSON object, got list" in exc.value.error


def test_arm_merge_reads_no_author_when_none_is_required(landing):
    """An interactive landing makes exactly the calls it made before the check existed."""
    ln, calls = landing(
        Fakes(gh_views={"state,title,body,reviewDecision": _OPEN}),
        arm_merge=True,
        await_merge=True,
    )
    merge.arm_merge(ln)
    assert not [c for c in calls if c[0] == "gh:author"], calls


def test_arm_merge_on_a_merged_pr_stays_a_no_op_under_a_required_author(landing):
    ln, calls = landing(
        Fakes(
            gh_views={"state,title,body,reviewDecision": {**_OPEN, "state": "MERGED"}}
        ),
        require_author="app/renovate",
    )
    merge.arm_merge(ln)
    assert not [c for c in calls if c[0] == "gh"]


@pytest.mark.parametrize(
    "states, verdict",
    [
        (["OPEN CONFLICTING dead", "OPEN CONFLICTING dead"], "merge-conflict"),
        (
            ["OPEN CONFLICTING dead", "OPEN MERGEABLE dead", "CLOSED MERGEABLE dead"],
            "refused",
        ),
        (["OPEN UNKNOWN dead", "OPEN UNKNOWN dead", "CLOSED UNKNOWN dead"], "refused"),
    ],
)
def test_only_a_settled_conflict_ends_the_wait(landing, states, verdict):
    ln, _ = landing(Fakes(gh_views={"state,mergeable,headRefOid": _wait(states)}))
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.rc == 1
    assert exc.value.verdict == verdict
    if verdict == "refused":
        assert "closed without merging" in exc.value.error


def test_a_red_pr_ci_ends_the_wait(landing):
    ln, _ = landing(
        Fakes(
            gh_views={"state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"])},
            await_ci=[(1, "dead1234: CI RED")],
        )
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.verdict == "pr-ci-red" and "dead1234: CI RED" in exc.value.error


@pytest.mark.parametrize("ci_rc", [75, 2])
def test_a_ci_answer_that_is_not_red_keeps_the_wait_going(landing, ci_rc):
    """75 is `pending`, the grace period; 2 is the disarmed gate, which checked nothing."""
    ln, _ = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": _wait(
                    ["OPEN MERGEABLE dead", "CLOSED MERGEABLE dead"]
                )
            },
            await_ci=[(ci_rc, "pending")],
        )
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (
        exc.value.verdict == "refused" and "closed without merging" in exc.value.error
    )


def test_the_merge_budget_ends_the_wait_with_its_own_verdict(landing):
    """Unreachable in the bash harness: LAND_MERGE_POLL=0 never advanced `waited`."""
    ln, _ = landing(
        Fakes(
            gh_views={"state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"])},
            await_ci=[(75, "pending")],
        ),
        merge_timeout=1,
        merge_poll=1,
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")


@pytest.mark.parametrize("ci_rc, names_the_rerun", [(75, True), (2, False)])
def test_the_merge_budget_names_a_head_ci_never_decided(
    landing, ci_rc, names_the_rerun
):
    """A cancelled or draft-skipped run on the head waits out the budget (#5931). The message
    is the only difference from a disarmed gate's timeout, so it is asserted."""
    ln, _ = landing(
        Fakes(
            gh_views={"state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"])},
            await_ci=[(ci_rc, "dead1234: no verdict after 0s")],
        ),
        merge_timeout=1,
        merge_poll=1,
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.verdict == "merge-timeout"
    assert ("gh run rerun" in exc.value.error) is names_the_rerun


def test_a_merged_pr_leaves_the_wait(landing, capsys):
    ln, _ = landing(
        Fakes(gh_views={"state,mergeable,headRefOid": _wait(["MERGED MERGEABLE dead"])})
    )
    merge.await_merge(ln)
    assert "merged after 0s" in capsys.readouterr().out


def test_the_merge_wait_never_hand_polls_ci():
    """merge.py is where someone would "improve" the wait by adding a `gh pr checks --watch`
    or a `gh run watch`. await_ci owns the CI verdict:
    it is one shot per poll, derived from the required checks, and its `pending` IS the
    grace period. A textual guard because the failure is a command that never appears in
    any test's call log until it is already in production."""
    text = Path(merge.__file__).read_text()
    assert "gh pr checks" not in text
    assert '"run"' not in text
    assert "await_ci" in text


@pytest.mark.parametrize("is_draft, ends_early", [(False, True), (True, False)])
def test_a_head_ci_skipped_as_a_draft_ends_the_wait_once_the_pr_is_ready(
    landing, capsys, is_draft, ends_early
):
    """#6077: PR #6069 was ready, its head's only run had skipped as a draft, and the wait sat
    out its whole budget. A PR still a draft gets its run once it is marked ready, so it waits.
    The ending's message is what tells the two apart, so it is asserted."""
    line = f"dead1234: no verdict after 0s — {merge.await_ci.DRAFT_SKIPPED}"
    ln, _ = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"]),
                "isDraft": {"isDraft": is_draft},
            },
            await_ci=[(75, line)],
        ),
        merge_timeout=merge.DRAFT_SKIPPED_GRACE_S * 3,
        merge_poll=merge.DRAFT_SKIPPED_GRACE_S // 3,
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert ("gh pr ready --undo 999" in exc.value.error) is ends_early
    assert ("still OPEN after" in exc.value.error) is not ends_early
    assert capsys.readouterr().out.count(merge.await_ci.DRAFT_SKIPPED) == 1


def _ready_toggles(calls):
    return [c[1] for c in calls if c[0] == "gh" and c[1][:2] == ("pr", "ready")]


def test_a_ready_pr_whose_head_skipped_as_a_draft_is_converted_once_and_the_wait_goes_on(
    landing, capsys
):
    """#6546: GitHub starts no run on the ready flip, so the landing converts the PR to draft
    and back itself. The second poll after the toggle reads a run, so the wait proceeds to the
    merge instead of ending merge-timeout."""
    skipped = f"dead1234: no verdict after 0s — {merge.await_ci.DRAFT_SKIPPED}"
    grace = merge.DRAFT_SKIPPED_GRACE_S
    # One skipped poll per third of the grace, then the run the toggle started goes green.
    polls = [(75, skipped)] * 4 + [(0, "dead1234: green")]
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": _wait(
                    ["OPEN MERGEABLE dead"] * 5 + ["MERGED MERGEABLE dead"]
                ),
                "isDraft": {"isDraft": False},
            },
            await_ci=polls,
        ),
        merge_timeout=grace * 3,
        merge_poll=grace // 3,
    )
    merge.await_merge(ln)
    assert _ready_toggles(calls) == [
        ("pr", "ready", "--undo", "999"),
        ("pr", "ready", "999"),
    ]
    assert "converting it to draft and back" in capsys.readouterr().out


def test_a_second_draft_skip_on_the_same_head_after_the_toggle_ends_merge_timeout(
    landing,
):
    """No loop: the toggle is once per head, and the dead end after it says it already ran."""
    skipped = f"dead1234: no verdict after 0s — {merge.await_ci.DRAFT_SKIPPED}"
    grace = merge.DRAFT_SKIPPED_GRACE_S
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"]),
                "isDraft": {"isDraft": False},
            },
            await_ci=[(75, skipped)],
        ),
        merge_timeout=grace * 10,
        merge_poll=grace // 3,
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert len(_ready_toggles(calls)) == 2
    assert "already converted it to draft and back" in exc.value.error


_DRAFT_RUN = {"id": 1, "status": "completed", "conclusion": "skipped"}


@pytest.mark.parametrize(
    "head_runs",
    [
        [_DRAFT_RUN, {"id": 2, "status": "queued", "conclusion": None}],
        [_DRAFT_RUN, {"id": 2, "status": "in_progress", "conclusion": None}],
        [_DRAFT_RUN, {"id": 2, "status": "completed", "conclusion": "cancelled"}],
        subprocess.CalledProcessError(1, "gh", stderr="HTTP 404: Not Found"),
        {"workflow_runs": []},
        ["not a run"],
    ],
    ids=["queued", "in-progress", "cancelled", "gh-fails", "not-a-list", "not-a-run"],
)
def test_a_ready_run_or_an_unreadable_run_list_stops_the_conversion(landing, head_runs):
    """#6566: the ready run's gate has no check run until its jobs finish, so await_ci reads
    only the draft run's skipped gate, and converting cancelled the ready run on 6443 and 6531.
    The conversion is what cancels, so a runs read that fails or parses wrong also stops it."""
    skipped = f"dead1234: no verdict after 0s — {merge.await_ci.DRAFT_SKIPPED}"
    grace = merge.DRAFT_SKIPPED_GRACE_S
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"]),
                "isDraft": {"isDraft": False},
            },
            await_ci=[(75, skipped)],
            head_runs=head_runs,
        ),
        merge_timeout=grace * 3,
        merge_poll=grace // 3,
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert _ready_toggles(calls) == []


def test_a_failed_conversion_ends_merge_timeout_and_names_a_draft_left_behind(landing):
    skipped = f"dead1234: no verdict after 0s — {merge.await_ci.DRAFT_SKIPPED}"
    grace = merge.DRAFT_SKIPPED_GRACE_S
    ln, _calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"]),
                "isDraft": {"isDraft": False},
            },
            await_ci=[(75, skipped)],
            gh_merge_rc=[0, 1],
        ),
        merge_timeout=grace * 10,
        merge_poll=grace // 3,
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert "left as a draft" in exc.value.error
