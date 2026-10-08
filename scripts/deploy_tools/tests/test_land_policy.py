"""The landing policy a lander unit sets: its checks, its head pin and its verdict file.

Each check is tested against a PR that passes every check, and refuses the one PR that breaks
it. A landing that sets no policy is tested to read none of the policy's fields, because
interactive sessions and renovate-agent's lander must land exactly as before.

Run: uv run pytest scripts/deploy_tools/tests/test_land_policy.py
"""

import pytest

import land
from _land_fakes import PRIMARY, Fakes, build_classifier, build_tools
from deploy_tools.land_lib import merge
from deploy_tools.land_lib.options import PRIMARY_ENV, REQUIRE_BRANCH_PREFIX_ENV
from deploy_tools.land_lib.outcome import Outcome

HEAD = "c" * 40
PREFIX = "worktree-claude+"
POLICY_FIELDS = "headRefOid,headRefName,baseRefName,isCrossRepository"
_ARM = {
    "state": "OPEN",
    "title": "Document the lander",
    "body": "",
    "reviewDecision": "REVIEW_REQUIRED",
}
_BRANCH = {
    "headRefOid": HEAD,
    "headRefName": "worktree-claude+lander-docs",
    "baseRefName": "master",
    "isCrossRepository": False,
}


def _fakes(branch=None, head_again=HEAD, arm=None, **fields) -> Fakes:
    return Fakes(
        gh_views={
            "state,title,body,reviewDecision": arm or _ARM,
            POLICY_FIELDS: {**_BRANCH, **(branch or {})},
            "headRefOid": {"headRefOid": head_again},
        },
        **fields,
    )


def _approval_paths(
    tmp_path, text="ansible/roles/setup/claude_code/\nansible/vars/secrets.yml\n"
):
    path = tmp_path / "approval-paths"
    path.write_text(text)
    return str(path)


def _arm(landing, tmp_path, fakes, **opts):
    opts.setdefault("require_branch_prefix", PREFIX)
    if (
        "approval_paths" not in opts
    ):  # not setdefault: its default would overwrite the file
        opts["approval_paths"] = _approval_paths(tmp_path)
    ln, calls = landing(fakes, arm_merge=True, await_merge=True, **opts)
    merge.arm_merge(ln)
    return ln, calls


def _merge_calls(calls):
    return [c for c in calls if c[0] == "gh" and "merge" in c[1]]


def test_a_landing_without_the_policy_reads_none_of_it(landing):
    """An interactive or Renovate landing makes no policy call and pins nothing."""
    ln, calls = landing(_fakes(), arm_merge=True, await_merge=True)
    merge.arm_merge(ln)
    assert ln.pinned_head == ""
    assert not [c for c in calls if c[0] in ("gh:files", f"gh:{POLICY_FIELDS}")]


def test_a_pr_the_policy_admits_is_pinned_to_the_head_it_checked(landing, tmp_path):
    ln, calls = _arm(landing, tmp_path, _fakes())
    assert ln.pinned_head == HEAD
    assert [c for c in calls if c[0] == "gh:files"]


# Each PR breaks exactly one check, and the refusal must say which.
REFUSALS = {
    "a branch outside the prefix": (
        {"branch": {"headRefName": "worktree-fix-3612"}},
        "outside worktree-claude+",
    ),
    "a branch in a fork": ({"branch": {"isCrossRepository": True}}, "not in this repo"),
    "a base other than master": ({"branch": {"baseRefName": "dev"}}, "targets dev"),
    "a path that needs approval": (
        {
            "pr_files": [
                {"filename": "ansible/roles/setup/claude_code/defaults/main.yml"}
            ]
        },
        "need the operator's approval: ansible/roles/setup/claude_code/defaults/main.yml",
    ),
    "a rename out of an approval path": (
        {
            "pr_files": [
                {
                    "filename": "docs/s.yml",
                    "previous_filename": "ansible/vars/secrets.yml",
                }
            ]
        },
        "ansible/vars/secrets.yml",
    ),
    "a held deployer": ({"state": {"hold_sha": "deadbeef"}}, "holding deadbeef"),
    "an unreadable hold": ({"state": {"hold_sha": None}}, "could not be read"),
    "a head that moved during the checks": (
        {"head_again": "d" * 40},
        "head moved during the checks",
    ),
    "a file list at GitHub's cap": (
        {"pr_files": [{"filename": f"docs/{i}.md"} for i in range(3000)]},
        "listing cap",
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_pr_breaking_one_check_is_refused_before_any_merge(landing, tmp_path, case):
    fields, expected = REFUSALS[case]
    with pytest.raises(Outcome) as exc:
        _arm(landing, tmp_path, _fakes(**fields))
    assert exc.value.rc == 1
    assert "refused by the landing policy" in exc.value.error
    assert expected in exc.value.error


@pytest.mark.parametrize(
    ("text", "expected"),
    [("# only a comment\n\n", "names no path"), (None, "unusable")],
    ids=["empty", "missing"],
)
def test_an_unusable_approval_list_refuses_rather_than_approving_everything(
    landing, tmp_path, text, expected
):
    path = _approval_paths(tmp_path, text) if text else str(tmp_path / "absent")
    with pytest.raises(Outcome) as exc:
        _arm(landing, tmp_path, _fakes(), approval_paths=path)
    assert expected in exc.value.error


def test_the_auto_merge_arm_is_pinned_to_the_checked_head(landing, tmp_path):
    """An approved PR takes the auto-merge path, which must carry the pin too."""
    _, calls = _arm(
        landing, tmp_path, _fakes(arm={**_ARM, "reviewDecision": "APPROVED"})
    )
    (call,) = _merge_calls(calls)
    assert call[1][-2:] == ("--match-head-commit", HEAD)


def test_a_head_that_moves_after_the_checks_stops_the_merge(landing):
    """The push lands after arm_merge read the head; await_merge must not merge it."""
    ln, calls = landing(
        Fakes(
            gh_views={
                # The second poll reads MERGED, so a missing head check ends in a merge, and the
                # test fails on "did not raise" rather than looping on a fake that never merges.
                "state,mergeable,headRefOid": [
                    {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "d" * 40},
                    {
                        "state": "MERGED",
                        "mergeable": "MERGEABLE",
                        "headRefOid": "d" * 40,
                    },
                ]
            }
        )
    )
    ln.pinned_head = HEAD
    ln.direct_merge_subject = "Document the lander"
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert "head moved from cccccccc to dddddddd" in exc.value.error
    assert not _merge_calls(calls) and not [c for c in calls if c[0] == "await_ci"]


def test_the_verdict_file_holds_one_line_naming_the_refusal(
    land_run, monkeypatch, tmp_path
):
    monkeypatch.setenv(REQUIRE_BRANCH_PREFIX_ENV, PREFIX)
    verdict = tmp_path / "999.verdict"
    rc, *_ = land_run(
        ["--arm-merge", "--await-merge", "--verdict-file", str(verdict)],
        _fakes(branch={"headRefName": "worktree-fix-3612"}),
    )
    assert rc == 1
    (line,) = verdict.read_text().splitlines()
    assert line.startswith("STOPPED: rc=1 PR #999 — refused by the landing policy:")


def test_the_verdict_file_reads_pending_until_the_landing_ends(monkeypatch, tmp_path):
    """Read through the `gh_json` seam, which the landing calls mid-run, before its verdict."""
    monkeypatch.setenv(PRIMARY_ENV, str(PRIMARY))
    verdict = tmp_path / "999.verdict"
    fakes = Fakes()
    tools, calls = build_tools(fakes)
    seen = []
    real_gh_json = tools.gh_json

    def spy(*args, **kwargs):
        seen.append(verdict.read_text())
        return real_gh_json(*args, **kwargs)

    tools.gh_json = spy
    land.main(
        ["--pr", "999", "--verdict-file", str(verdict)],
        tools=tools,
        classifier=build_classifier(fakes, calls),
    )
    assert seen and set(seen) == {"PENDING\n"}
    (line,) = verdict.read_text().splitlines()
    assert line.startswith("VERDICT: ")
