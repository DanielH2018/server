"""Every land.sh refusal that exits 1 prints one `VERDICT:` line (#4451).

A landing that stopped without one reads to `land_probe` only through its `land:` stderr line,
and land.py's docstring promises verdicts on stdout. Each case below drives one refusal the
issue names and asserts the exit code and the refusal's own wording BEFORE the verdict, so a
case that reaches an earlier refusal fails on the wording rather than passing for the wrong
reason. The verdict's name is not pinned: any member of the closed set satisfies the contract.

Run: uv run pytest scripts/deploy_tools/tests/test_land_every_exit_names_a_verdict.py
"""

import re
import subprocess
from pathlib import Path

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import classify, merge
from deploy_tools.land_lib.options import REQUIRE_BRANCH_PREFIX_ENV
from deploy_tools.land_lib.outcome import VERDICTS, Outcome

HEAD = "c" * 40
_OPEN = {
    "state": "OPEN",
    "title": "Document the lander",
    "body": "",
    "reviewDecision": "REVIEW_REQUIRED",
}
_ARM_FIELDS = "state,title,body,reviewDecision"
_VERDICT_LINE = re.compile(r"^VERDICT: (\S+) \(", re.MULTILINE)


def _closed_before_the_arm(landing):
    ln, _ = landing(Fakes(gh_views={_ARM_FIELDS: {**_OPEN, "state": "CLOSED"}}))
    merge.arm_merge(ln)


def _closed_during_the_wait(landing):
    ln, _ = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": {
                    "state": "CLOSED",
                    "mergeable": "UNKNOWN",
                    "headRefOid": HEAD,
                }
            }
        )
    )
    merge.await_merge(ln)


def _changes_requested(landing):
    ln, _ = landing(
        Fakes(gh_views={_ARM_FIELDS: {**_OPEN, "reviewDecision": "CHANGES_REQUESTED"}}),
        arm_merge=True,
        await_merge=True,
    )
    merge.arm_merge(ln)


def _bypass_without_await(landing):
    ln, _ = landing(Fakes(gh_views={_ARM_FIELDS: _OPEN}), arm_merge=True)
    merge.arm_merge(ln)


def _head_moved_after_the_policy(landing):
    moved = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "d" * 40}
    ln, _ = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": [moved, {**moved, "state": "MERGED"}]
            }
        )
    )
    ln.pinned_head = HEAD
    ln.direct_merge_subject = "Document the lander"
    merge.await_merge(ln)


def _private_repo(landing):
    ln, _ = landing(
        Fakes(gh_views={_ARM_FIELDS: _OPEN}, repo={"visibility": "private"}),
        arm_merge=True,
        await_merge=True,
    )
    merge.arm_merge(ln)


def _another_author(landing):
    ln, _ = landing(
        Fakes(
            gh_views={
                _ARM_FIELDS: _OPEN,
                "author": {"author": {"is_bot": False, "login": "someone"}},
            }
        ),
        arm_merge=True,
        require_author="app/renovate",
    )
    merge.arm_merge(ln)


def _stray_closing_ref(landing):
    ln, _ = landing(
        Fakes(
            gh_views={_ARM_FIELDS: {**_OPEN, "body": "Filed and not fixed: #2509\n"}}
        ),
        arm_merge=True,
    )
    merge.arm_merge(ln)


def _unreadable_pr(landing):
    ln, _ = landing()

    def gh_json(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 404")

    ln.tools.gh_json = gh_json
    ln.view("state")


def _unfetchable_master(landing):
    ln, _ = landing(Fakes(fetch_rc=1))
    ln.fetch_branch()


def _no_merge_commit(landing):
    ln, _ = landing(Fakes(gh_views={"mergeCommit": {"mergeCommit": None}}))
    classify.resolve(ln)


REFUSALS = {
    "closed before the arm": (_closed_before_the_arm, "closed without merging"),
    "closed during the merge wait": (_closed_during_the_wait, "closed without merging"),
    "changes requested": (_changes_requested, "has changes requested"),
    "bypass-only merge without --await-merge": (
        _bypass_without_await,
        "only through a ruleset bypass",
    ),
    "head moved after the policy checked it": (
        _head_moved_after_the_policy,
        "head moved from cccccccc to dddddddd",
    ),
    "private repo": (_private_repo, "repo is private"),
    "another author": (_another_author, "someone, not app/renovate"),
    "stray closing keyword": (_stray_closing_ref, "Filed and not fixed: #2509"),
    "unreadable PR": (_unreadable_pr, "HTTP 404"),
    "unfetchable master": (_unfetchable_master, "could not fetch origin/master"),
    "no merge commit": (_no_merge_commit, "no merge commit"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_an_exit_1_refusal_names_a_verdict(landing, case):
    drive, wording = REFUSALS[case]
    with pytest.raises(Outcome) as exc:
        drive(landing)
    assert exc.value.rc == 1
    assert wording in exc.value.error
    assert exc.value.verdict in VERDICTS


def test_a_policy_refusal_prints_a_verdict_line_and_records_it(
    land_run, monkeypatch, tmp_path
):
    """The user-visible contract: stdout and the lander unit's verdict file both carry it."""
    monkeypatch.setenv(REQUIRE_BRANCH_PREFIX_ENV, "worktree-claude+")
    verdict_file = tmp_path / "999.verdict"
    fakes = Fakes(
        gh_views={
            _ARM_FIELDS: _OPEN,
            "headRefOid,headRefName,baseRefName,isCrossRepository": {
                "headRefOid": HEAD,
                "headRefName": "worktree-fix-3612",
                "baseRefName": "master",
                "isCrossRepository": False,
            },
            "headRefOid": {"headRefOid": HEAD},
        }
    )
    rc, out, err, *_ = land_run(
        ["--arm-merge", "--await-merge", "--verdict-file", str(verdict_file)], fakes
    )
    assert rc == 1
    assert "refused by the landing policy" in err
    printed = _VERDICT_LINE.findall(out)
    assert len(printed) == 1 and printed[0] in VERDICTS, out
    (recorded,) = verdict_file.read_text().splitlines()
    assert recorded.startswith("VERDICT: ")


def test_a_missing_primary_checkout_prints_a_verdict_line(land_run):
    rc, out, err, *_ = land_run([], primary=Path("/nonexistent"))
    assert rc == 1
    assert "cannot cd to /nonexistent" in err
    printed = _VERDICT_LINE.findall(out)
    assert len(printed) == 1 and printed[0] in VERDICTS, out
