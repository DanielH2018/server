"""--await-merge on a PR whose base master has moved past since its CI ran (issue #4013).

A green head whose merge base is behind master is not merged: the branch is updated once and
the merge waits for the updated head to go green. Each behaviour is tested against its
counterpart: a stale base against an up-to-date one, an update GitHub accepts against one it
refuses, and a head GitHub has already moved against one it has not.

Run: uv run pytest scripts/deploy_tools/tests/test_land_stale_base.py
"""

import subprocess

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import merge
from deploy_tools.land_lib.outcome import Outcome

OLD, NEW = "a" * 40, "b" * 40
PREFIX = "worktree-claude+"
# What master changed between #2922's merge base and the commit before its merge
# (cc2a71fd8), as GitHub's compare endpoint lists it, cut to the path that turned master red:
# a role CLAUDE.md the PR's new size test read, in a directory the PR never touched.
INCIDENT_2922 = [
    ".claude/hooks/session-health.py",
    "ansible/roles/k8s/claude-otel/CLAUDE.md",
    "ansible/tests/_ratchet.py",
]


def _polls(*heads: str) -> list[dict]:
    """One `state,mergeable,headRefOid` answer per head, then the PR reading MERGED."""
    open_ = [
        {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": h} for h in heads
    ]
    return [
        *open_,
        {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": heads[-1]},
    ]


def _landing(landing, polls, compare, gh_rc=(0,), **opts):
    ln, calls = landing(
        Fakes(
            gh_views={"state,mergeable,headRefOid": polls},
            compare=compare,
            gh_merge_rc=list(gh_rc),
        ),
        **opts,
    )
    ln.direct_merge_subject = "Document the lander"
    return ln, calls


def _gh_writes(calls) -> list[str]:
    """Each PUT the landing sent, as `<endpoint> <pinned sha>`."""
    writes = []
    for name, args, _ in calls:
        if name == "gh":
            pin = next(a for a in args if a.startswith(("sha=", "expected_head_sha=")))
            writes.append(f"{args[3].rsplit('/', 1)[-1]} {pin.split('=')[1][:4]}")
    return writes


def test_a_stale_base_updates_the_branch_and_merges_the_updated_head(landing):
    ln, calls = _landing(landing, _polls(OLD, NEW), [INCIDENT_2922])
    merge.await_merge(ln)
    assert _gh_writes(calls) == ["update-branch aaaa", "merge bbbb"]
    assert len([c for c in calls if c[0] == "gh:compare"]) == 1


def test_an_up_to_date_base_merges_without_an_update(landing):
    ln, calls = _landing(landing, _polls(OLD), [[]])
    merge.await_merge(ln)
    assert _gh_writes(calls) == ["merge aaaa"]
    (compare,) = [c for c in calls if c[0] == "gh:compare"]
    assert compare[1][1] == f"repos/{{owner}}/{{repo}}/compare/{OLD}...master"


def test_the_old_head_is_not_merged_while_github_applies_the_update(landing):
    """GitHub updates a branch asynchronously, so the next poll can still show the old head."""
    ln, calls = _landing(landing, _polls(OLD, OLD, NEW), [INCIDENT_2922])
    merge.await_merge(ln)
    assert _gh_writes(calls) == ["update-branch aaaa", "merge bbbb"]
    assert [c[1][0][:4] for c in calls if c[0] == "await_ci"] == ["aaaa", "bbbb"]


def test_a_landing_updates_the_branch_once_even_if_master_moves_again(landing):
    """Chasing master would end in merge-timeout on a busy day: one re-run per landing."""
    ln, calls = _landing(
        landing, _polls(OLD, NEW), [INCIDENT_2922, ["docs/landing.md"]]
    )
    merge.await_merge(ln)
    assert _gh_writes(calls) == ["update-branch aaaa", "merge bbbb"]


def test_a_refused_update_dies_without_merging(landing):
    ln, calls = _landing(landing, _polls(OLD), [INCIDENT_2922], gh_rc=(1,))
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.rc == 1
    assert "rebase it onto origin/master" in exc.value.error
    assert _gh_writes(calls) == ["update-branch aaaa"]


def test_an_unreadable_compare_dies_without_merging(landing):
    failed = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
    ln, calls = _landing(landing, _polls(OLD), [failed])
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert "could not compare aaaaaaaa with master: HTTP 502" in exc.value.error
    assert _gh_writes(calls) == []


def test_the_policy_checks_and_pins_the_head_its_own_update_made(landing):
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
    merge.await_merge(ln)
    assert ln.pinned_head == NEW
    assert _gh_writes(calls) == ["update-branch aaaa", "merge bbbb"]


def test_a_head_the_operator_approved_merges_without_an_update(landing, tmp_path):
    """The update would make a head the approval does not name, and the policy refuses that."""
    approval_paths = tmp_path / "approval-paths"
    approval_paths.write_text("scripts/deploy_tools/land\n")
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
                "state,mergeable,headRefOid": _polls(OLD),
            },
            pr_files=[{"filename": "scripts/deploy_tools/land_lib/merge.py"}],
            pr_reviews=[
                {
                    "user": {"login": "operator-login"},
                    "state": "APPROVED",
                    "commit_id": OLD,
                    "submitted_at": "2026-10-05T20:00:00Z",
                }
            ],
            compare=[INCIDENT_2922],
        ),
        arm_merge=True,
        await_merge=True,
        require_branch_prefix=PREFIX,
        approval_paths=str(approval_paths),
        approver="operator-login",
    )
    merge.arm_merge(ln)
    merge.await_merge(ln)
    assert ln.approved_head == OLD
    assert _gh_writes(calls) == ["merge aaaa"]
    assert not [c for c in calls if c[0] == "gh:compare"]
