"""--await-merge on a green head whose base master has moved past since its CI ran.

Inside the landing turn a head gets its run on the master its merge lands on
(`test_land_turn.py`, `test_land_rejoin_line.py`). A stale green head that reaches the merge
anyway merges as it stands, unless the operator approved it: an approved head is refused when
master changed a path the PR changes, because an update would void the approval.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_stale_base.py
"""

import subprocess

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import land_tested_base, merge, stale_base
from deploy_tools.land_lib.outcome import Outcome

OLD, NEW = "a" * 40, "b" * 40
# The master commit OLD's green CI run tested it against, newer than OLD's merge base.
TESTED = "c" * 40
PREFIX = "worktree-claude+"
INCIDENT_2922 = [
    ".claude/hooks/session-health.py",
    "k8s/roles/claude-otel/CLAUDE.md",
    "tests/_ratchet.py",
]
# The approved PR's own file, which a landing policy's approval list names.
APPROVED_FILE = "scripts/deploy_tools/land_lib/merge.py"


def _polls(*heads: str) -> list[dict]:
    """One `state,mergeable,headRefOid` answer per head, then the PR reading MERGED."""
    open_ = [
        {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": h} for h in heads
    ]
    return [
        *open_,
        {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": heads[-1]},
    ]


def _gh_writes(calls) -> list[str]:
    """Each PUT the landing sent, as `<endpoint> <pinned sha>`."""
    writes = []
    for name, args, _ in calls:
        if name == "gh":
            pin = next(a for a in args if a.startswith(("sha=", "expected_head_sha=")))
            writes.append(f"{args[3].rsplit('/', 1)[-1]} {pin.split('=')[1][:4]}")
    return writes


def _compares(calls) -> list[str]:
    return [c[1][1] for c in calls if c[0] == "gh:compare"]


def test_a_stale_head_nobody_approved_merges_as_it_stands_without_a_compare(landing):
    """Master's own run tests it; the landing spends no read on a base it will not act on."""
    ln, calls = landing(
        Fakes(
            gh_views={"state,mergeable,headRefOid": _polls(OLD)},
            compare=[INCIDENT_2922],
            pr_files=[{"status": "modified", "filename": INCIDENT_2922[1]}],
        )
    )
    ln.direct_merge_subject = "Document the lander"
    merge.await_merge(ln)
    assert _gh_writes(calls) == ["merge aaaa"]
    assert _compares(calls) == []


@pytest.mark.parametrize(
    ("tested_base", "since"),
    [
        pytest.param({OLD: TESTED}, TESTED, id="the-tested-base"),
        pytest.param({}, OLD, id="the-merge-base-when-the-log-cannot-name-it"),
    ],
)
def test_master_paths_are_compared_from_the_base_the_green_run_tested(
    landing, tested_base, since
):
    ln, calls = landing(Fakes(compare=[INCIDENT_2922], tested_base=tested_base))
    assert stale_base.master_paths_since(ln, OLD) == INCIDENT_2922
    assert _compares(calls) == [f"repos/{{owner}}/{{repo}}/compare/{since}...master"]


def _tested_base(runs, jobs, log):
    """`tested_base(OLD)` over fake gh reads: the runs list, the jobs list and the job log."""

    def gh_json(*args, **_):
        endpoint = args[1]
        if "/workflows/ci.yml/runs" in endpoint:
            assert f"head_sha={OLD}" in endpoint
            return {"workflow_runs": runs}
        assert endpoint.endswith("/actions/runs/7/jobs")
        return jobs

    def gh(*args, **_):
        if isinstance(log, Exception):
            raise log
        return subprocess.CompletedProcess(args, 0, stdout=log)

    return land_tested_base.tested_base(OLD, gh_json=gh_json, gh=gh)


def _runs(*conclusions_by_id):
    """REST workflow-run rows, newest first, from `(id, conclusion)` pairs."""
    return [
        {"id": run_id, "status": "completed", "conclusion": conclusion}
        for run_id, conclusion in conclusions_by_id
    ]


GREEN_7 = _runs((7, "success"))

CHECKOUT_LOG = (
    "2026-10-17T22:19:21Z HEAD is now at db8b8d59f Merge {head} into {base}\n"
    "2026-10-17T22:19:22Z Download action repository 'actions/setup-python'\n"
)


def test_tested_base_is_the_base_in_the_checkout_logs_merge_line():
    log = CHECKOUT_LOG.format(head=OLD, base=TESTED)
    assert _tested_base(_runs((7, "success"), (6, "success")), [101], log) == TESTED


def test_tested_base_reads_the_newest_green_run_past_a_red_one():
    log = CHECKOUT_LOG.format(head=OLD, base=TESTED)
    assert _tested_base(_runs((8, "failure"), (7, "success")), [101], log) == TESTED


@pytest.mark.parametrize(
    ("runs", "jobs", "log"),
    [
        pytest.param([], [], "", id="no-run"),
        pytest.param(_runs((8, "failure")), [], "", id="no-green-run"),
        pytest.param(GREEN_7, [], "", id="no-green-job"),
        pytest.param(
            GREEN_7, [101], "no merge line here\n", id="log-without-merge-line"
        ),
        pytest.param(
            GREEN_7,
            [101],
            CHECKOUT_LOG.format(head=NEW, base=TESTED),
            id="log-of-another-head",
        ),
        pytest.param(
            GREEN_7, [101], subprocess.CalledProcessError(1, ["gh"]), id="log-expired"
        ),
    ],
)
def test_tested_base_is_none_when_the_run_log_cannot_name_it(runs, jobs, log):
    assert _tested_base(runs, jobs, log) is None


def test_the_policy_checks_and_pins_the_head_the_turns_own_update_made(landing):
    ln, calls = landing(
        Fakes(
            gh_views={
                # NEW twice: the poll that first shows it ends in the policy check.
                "state,mergeable,headRefOid": _polls(OLD, NEW, NEW),
                "headRefOid,headRefName,baseRefName,isCrossRepository": {
                    "headRefOid": NEW,
                    "headRefName": "worktree-claude+lander-docs",
                    "baseRefName": "master",
                    "isCrossRepository": False,
                },
                "headRefOid": {"headRefOid": NEW},
            },
            compare=[INCIDENT_2922],
        ),
        require_branch_prefix=PREFIX,
    )
    ln.pinned_head = OLD
    ln.direct_merge_subject = "Document the lander"
    merge.await_merge(ln, lambda _draft: (True, 0))
    assert ln.pinned_head == NEW
    assert _gh_writes(calls) == ["update-branch aaaa", "merge bbbb"]


def _approved_landing(landing, tmp_path, compare, mergeable="MERGEABLE", pr_files=None):
    """A policy landing whose head OLD the operator approved, armed and ready to await."""
    approval_paths = tmp_path / "approval-paths"
    approval_paths.write_text("scripts/deploy_tools/land\n")
    polls = _polls(OLD)
    polls[0]["mergeable"] = mergeable
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title,body,reviewDecision": {
                    "state": "OPEN",
                    "title": "Document the lander",
                    "body": "",
                    "reviewDecision": "APPROVED",
                },
                "headRefOid,headRefName,baseRefName,isCrossRepository": {
                    "headRefOid": OLD,
                    "headRefName": "worktree-claude+lander-docs",
                    "baseRefName": "master",
                    "isCrossRepository": False,
                },
                "headRefOid": {"headRefOid": OLD},
                "state,mergeable,headRefOid": polls,
            },
            pr_files=pr_files or [{"status": "modified", "filename": APPROVED_FILE}],
            pr_reviews=[
                {
                    "user": {"login": "operator-login"},
                    "state": "APPROVED",
                    "commit_id": OLD,
                    "submitted_at": "2026-10-05T20:00:00Z",
                }
            ],
            compare=compare,
        ),
        arm_merge=True,
        await_merge=True,
        require_branch_prefix=PREFIX,
        approval_paths=str(approval_paths),
        approver="operator-login",
    )
    merge.arm_merge(ln)
    assert ln.approved_head == OLD
    return ln, calls


@pytest.mark.parametrize(
    ("compare", "mergeable", "pr_files"),
    [
        pytest.param([*INCIDENT_2922, APPROVED_FILE], "MERGEABLE", None, id="overlap"),
        pytest.param(INCIDENT_2922, "CONFLICTING", None, id="conflicting"),
        pytest.param(
            [f"docs/{i}.md" for i in range(300)], "MERGEABLE", None, id="capped-compare"
        ),
        pytest.param(
            INCIDENT_2922,
            "MERGEABLE",
            [
                {
                    "status": "renamed",
                    "filename": APPROVED_FILE,
                    "previous_filename": "k8s/roles/claude-otel/CLAUDE.md",
                }
            ],
            id="pr-renamed-a-moved-path",
        ),
    ],
)
def test_a_stale_approved_head_is_refused_without_an_update(
    landing, tmp_path, compare, mergeable, pr_files
):
    """The update would make a head the approval does not name; merging it is untested."""
    ln, calls = _approved_landing(landing, tmp_path, [compare], mergeable, pr_files)
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.rc == 1
    assert "get the new head approved" in exc.value.error
    assert _gh_writes(calls) == []


def test_an_up_to_date_approved_head_merges(landing, tmp_path):
    ln, calls = _approved_landing(landing, tmp_path, [[]])
    merge.await_merge(ln)
    assert _gh_writes(calls) == ["merge aaaa"]


def test_an_approved_head_whose_files_master_did_not_touch_merges(landing, tmp_path):
    """The accepted risk of the approval ruling, traded for a re-approval."""
    ln, calls = _approved_landing(landing, tmp_path, [INCIDENT_2922])
    merge.await_merge(ln)
    assert _gh_writes(calls) == ["merge aaaa"]


def test_an_unreadable_compare_refuses_an_approved_head_without_merging(
    landing, tmp_path
):
    failed = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
    ln, calls = _approved_landing(landing, tmp_path, [failed])
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert "could not compare aaaaaaaa with master: HTTP 502" in exc.value.error
    assert _gh_writes(calls) == []
