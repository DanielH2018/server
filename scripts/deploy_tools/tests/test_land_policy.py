"""The landing policy a lander unit sets: its checks, its head pin and its verdict file.

Each check is tested against a PR that passes every check, and refuses the one PR that breaks
it. A landing that sets no policy is tested to read none of the policy's fields, because
interactive sessions and renovate-agent's lander must land exactly as before.

Run: uv run pytest scripts/deploy_tools/tests/test_land_policy.py
"""

import sys
from types import ModuleType

import pytest

import land  # loads the landing's modules, which gate_hits reads
from _land_fakes import PRIMARY, Fakes, build_classifier, build_tools
from deploy_tools.land_lib import merge, policy
from deploy_tools.land_lib.options import PRIMARY_ENV, REQUIRE_BRANCH_PREFIX_ENV
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.pr_json import parse_file

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
    # Not on the approval list: the landing process imported it, which is what refuses it.
    "a module the landing imports": (
        {"pr_files": [{"filename": "scripts/lib/gh.py"}]},
        "need the operator's approval: scripts/lib/gh.py",
    ),
    "a held deployer": ({"state": {"hold_sha": "deadbeef"}}, "holding deadbeef"),
    "an unreadable hold": ({"state": {"hold_sha": None}}, "could not be read"),
    "a head that moved during the checks": (
        {"head_again": "d" * 40},
        "head moved during the checks",
    ),
    "a file entry of the wrong shape": (
        {"pr_files": [{"filename": 7}]},
        "could not list the PR's files: unparseable gh output",
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


def test_an_approved_pr_merges_directly_at_the_checked_head(landing, tmp_path):
    """An approved agent PR still needs the fence's bypass, so it takes the pinned REST merge.

    Arming auto-merge left #3911 BLOCKED until a hand merge.
    """
    fakes = _fakes(arm={**_ARM, "reviewDecision": "APPROVED"})
    fakes.gh_views["state,mergeable,headRefOid"] = [
        {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": HEAD},
        {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": HEAD},
    ]
    ln, calls = _arm(landing, tmp_path, fakes)
    assert not [c for c in calls if c[0] == "gh" and "--auto" in c[1]]
    merge.await_merge(ln)
    (call,) = [c for c in calls if c[0] == "gh" and "PUT" in c[1]]
    assert call[1][:4] == ("api", "-X", "PUT", "repos/{owner}/{repo}/pulls/999/merge")
    assert f"sha={HEAD}" in call[1]


def test_the_auto_merge_arm_is_pinned_to_the_checked_head(landing, tmp_path):
    """A PR with no review decision takes the auto-merge path, which must carry the pin too.

    An empty decision is the only one left on that path: REVIEW_REQUIRED and APPROVED merge
    directly, and CHANGES_REQUESTED is refused.
    """
    _, calls = _arm(landing, tmp_path, _fakes(arm={**_ARM, "reviewDecision": ""}))
    (call,) = _merge_calls(calls)
    assert "--auto" in call[1]
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


APPROVER = "operator-login"
_CHANGES_AN_APPROVAL_PATH = [
    {"filename": "ansible/roles/setup/claude_code/defaults/main.yml"}
]


def _review(state, at="2026-10-05T20:00:00Z", commit=HEAD, login=APPROVER):
    return {
        "user": {"login": login},
        "state": state,
        "commit_id": commit,
        "submitted_at": at,
    }


def _arm_approval_path(landing, tmp_path, reviews, **opts):
    fakes = _fakes(pr_files=_CHANGES_AN_APPROVAL_PATH, pr_reviews=reviews)
    return _arm(landing, tmp_path, fakes, **opts)


def test_without_an_approver_no_review_is_read(landing, tmp_path):
    fakes = _fakes(pr_files=_CHANGES_AN_APPROVAL_PATH, pr_reviews=[_review("APPROVED")])
    ln, calls = landing(
        fakes,
        arm_merge=True,
        await_merge=True,
        require_branch_prefix=PREFIX,
        approval_paths=_approval_paths(tmp_path),
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert "need the operator's approval" in exc.value.error
    assert not [c for c in calls if c[0] == "gh:reviews"]


def test_the_approvers_approval_of_the_head_lifts_the_refusal(landing, tmp_path):
    ln, calls = _arm_approval_path(
        landing, tmp_path, [_review("APPROVED")], approver=APPROVER
    )
    assert ln.pinned_head == HEAD
    assert [c for c in calls if c[0] == "gh:reviews"]


# Each review history leaves the head unapproved by the approver, and the refusal says why.
UNAPPROVED = {
    "no review": ([], "has not approved it"),
    "another login's approval": (
        [_review("APPROVED", login="someone-else")],
        "has not approved it",
    ),
    "an approval of an older commit": (
        [_review("APPROVED", commit="a" * 40)],
        "approved aaaaaaaa, not the head cccccccc",
    ),
    "changes requested after the approval": (
        [_review("APPROVED"), _review("CHANGES_REQUESTED", at="2026-10-05T21:00:00Z")],
        "latest review is changes_requested",
    ),
    # Listed out of order, so only the timestamps say which came last.
    "changes requested after the approval, listed first": (
        [_review("CHANGES_REQUESTED", at="2026-10-05T21:00:00Z"), _review("APPROVED")],
        "latest review is changes_requested",
    ),
    "a dismissed approval": ([_review("DISMISSED")], "latest review is dismissed"),
}


def test_a_review_of_the_wrong_shape_refuses_as_unparseable(landing, tmp_path):
    reviews = [{**_review("APPROVED"), "user": APPROVER}]
    with pytest.raises(Outcome) as exc:
        _arm_approval_path(landing, tmp_path, reviews, approver=APPROVER)
    assert "could not list the PR's reviews: unparseable gh output" in exc.value.error


@pytest.mark.parametrize("case", sorted(UNAPPROVED))
def test_a_review_history_without_a_head_approval_still_refuses(
    landing, tmp_path, case
):
    reviews, expected = UNAPPROVED[case]
    with pytest.raises(Outcome) as exc:
        _arm_approval_path(landing, tmp_path, reviews, approver=APPROVER)
    assert "need the operator's approval" in exc.value.error
    assert expected in exc.value.error


@pytest.mark.parametrize(
    "reviews",
    [
        [_review("APPROVED"), _review("COMMENTED", at="2026-10-05T21:00:00Z")],
        [_review("CHANGES_REQUESTED", at="2026-10-05T19:00:00Z"), _review("APPROVED")],
    ],
    ids=["a comment after the approval", "an approval after changes requested"],
)
def test_the_approvers_latest_verdict_decides(reviews):
    assert policy.approval_problem(reviews, APPROVER, HEAD) == ""


def _module(name, source=None):
    module = ModuleType(name)
    module.__file__ = source
    return module


def _gate_hits(*paths, previous=None):
    """`gate_hits` over a checkout that loaded `lib.gh` and the stdlib `json`."""
    checkout = policy.CHECKOUT
    modules = {
        "lib": _module("lib"),
        "lib.gh": _module("lib.gh", str(checkout / "scripts/lib/gh.py")),
        "json": _module("json", "/usr/lib/python3.14/json/__init__.py"),
    }
    entries = [{"filename": p} for p in paths]
    if previous:
        entries.append({"filename": "docs/moved.py", "previous_filename": previous})
    files = [parse_file(e) for e in entries]
    return policy.gate_hits(files, checkout, modules, [str(checkout / "scripts")])


def test_a_module_the_gate_loaded_is_flagged():
    assert _gate_hits("scripts/lib/gh.py", "docs/landing.md") == ["scripts/lib/gh.py"]


def test_a_path_the_gate_never_loaded_is_clean():
    assert _gate_hits("scripts/lib/unused.py", "scripts/dev/findings.py") == []


@pytest.mark.parametrize(
    "path",
    ["scripts/json.py", "scripts/lib/__init__.py", "scripts/sitecustomize.py"],
    ids=["shadows-stdlib", "namespace-to-package", "startup-hook"],
)
def test_a_new_file_that_would_import_under_a_loaded_name_is_flagged(path):
    assert _gate_hits(path) == [path]


def test_a_rename_out_of_a_loaded_module_is_flagged():
    assert _gate_hits(previous="scripts/lib/gh.py") == ["scripts/lib/gh.py"]


def test_the_real_landing_process_counts_its_shared_modules():
    """What `import land` loads is the closure, so these members must be in it."""
    members = [
        "scripts/lib/gh.py",
        "scripts/lib/repo_paths.py",
        "scripts/deploy_tools/land_lib/policy.py",
        "scripts/deploy_tools/narrow_paths.py",
        "ansible/roles/setup/gitops_deploy/files/deploy_logic.py",
    ]
    files = [parse_file({"filename": p}) for p in members]
    assert policy.gate_hits(files, policy.CHECKOUT, sys.modules, sys.path) == members
