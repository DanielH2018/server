"""publish_pr.py `unlanded`: the guard the three crons run before they commit, and the
backstop merge of a PR that waits only on the review gate.
"""

import subprocess

import pytest

import publish_pr
from _publish_pr_fakes import TIMEOUT, Recorder, cp


# --- unlanded: is a previous run's branch still on origin --------------------------------------

_HEAD = "9f8e7d6\trefs/heads/docs-refresh/2026-09-03-0600"


def _unlanded(rec: Recorder) -> publish_pr.PublishOutcome:
    return publish_pr.unlanded("docs-refresh/", rec.tools())


def test_no_remote_head_means_nothing_is_unlanded():
    out = _unlanded(Recorder({"git ls-remote": cp(0, out="")}))
    assert out.rc == publish_pr.UNLANDED_NOTHING
    assert out.message == ""


def test_an_unreachable_origin_fails_closed():
    """`|| true` here would read an unreachable origin as "no stale branch" and publish."""
    rec = Recorder({"git ls-remote": cp(128, err="Could not resolve host: github.com")})
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_ORIGIN_UNREADABLE
    assert "Could not resolve host" in out.message
    assert not any(c[0] == "gh" for c in rec.calls), (
        "the PR lookup must not run when origin could not be read"
    )


def test_a_remote_head_with_an_open_pr_is_the_benign_code():
    rec = Recorder(
        {
            "git ls-remote": cp(0, out=_HEAD),
            "gh pr list": cp(
                0, out='[{"number": 41, "headRefName": "docs-refresh/2026-09-03-0600"}]'
            ),
        }
    )
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_PR_OPEN
    assert "PR #41" in out.message
    assert out.branch == "docs-refresh/2026-09-03-0600"


_OPEN_PR = '[{"number": 41, "headRefName": "docs-refresh/2026-09-03-0600"}]'


def _view(review: str, *conclusions: str) -> subprocess.CompletedProcess[str]:
    rollup = ",".join(f'{{"conclusion": "{c}"}}' for c in conclusions)
    return cp(
        0,
        out=f'{{"headRefOid": "abc1234def", "reviewDecision": "{review}", '
        f'"statusCheckRollup": [{rollup}]}}',
    )


def test_a_green_pr_waiting_only_on_review_is_merged_at_its_head():
    rec = Recorder(
        {
            "git ls-remote": cp(0, out=_HEAD),
            "gh pr list": cp(0, out=_OPEN_PR),
            "gh pr view": _view("REVIEW_REQUIRED", "SUCCESS", "SKIPPED"),
        }
    )
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_PR_OPEN, "this run still skips on it"
    assert "merged at abc1234d" in out.message
    merge = [c for c in rec.calls if c[:2] == ("gh", "api")]
    assert merge == [
        (
            "gh",
            "api",
            "-X",
            "PUT",
            "repos/{owner}/{repo}/pulls/41/merge",
            "-f",
            "merge_method=squash",
            "-f",
            "sha=abc1234def",
        )
    ]


@pytest.mark.parametrize(
    "view",
    [
        _view("REVIEW_REQUIRED", "SUCCESS", ""),
        _view("REVIEW_REQUIRED", "SUCCESS", "FAILURE"),
        _view("REVIEW_REQUIRED"),
        _view("APPROVED", "SUCCESS"),
        cp(1, err="HTTP 502"),
    ],
    ids=[
        "check-pending",
        "check-failed",
        "no-checks-yet",
        "not-review-gated",
        "view-failed",
    ],
)
def test_a_pr_that_is_not_stalled_is_left_to_its_landing(view):
    rec = Recorder(
        {
            "git ls-remote": cp(0, out=_HEAD),
            "gh pr list": cp(0, out=_OPEN_PR),
            "gh pr view": view,
        }
    )
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_PR_OPEN
    assert (
        out.message
        == "PR #41 from a previous run is still open (docs-refresh/2026-09-03-0600)"
    )
    assert not any(c[:2] == ("gh", "api") for c in rec.calls)


def test_a_refused_backstop_merge_is_reported():
    rec = Recorder(
        {
            "git ls-remote": cp(0, out=_HEAD),
            "gh pr list": cp(0, out=_OPEN_PR),
            "gh pr view": _view("REVIEW_REQUIRED", "SUCCESS"),
            "gh api -X": cp(1, err="Head branch was modified"),
        }
    )
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_PR_OPEN
    assert "the merge was refused" in out.message
    assert "Head branch was modified" in out.message


def test_a_remote_head_with_no_open_pr_is_the_stuck_code():
    """The state a failed `gh pr create` leaves: a branch on origin, no PR, no local trace."""
    rec = Recorder({"git ls-remote": cp(0, out=_HEAD), "gh pr list": cp(0, out="[]")})
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_NO_PR
    assert "NO open PR" in out.message


def test_an_open_pr_on_a_sibling_branch_does_not_clear_an_orphan():
    """Two heads under one prefix is the state orphans accumulate into.

    Matching the PR by prefix would return the sibling's number, downgrade the stuck state to
    the benign one, and name the wrong branch while doing it.
    """
    rec = Recorder(
        {
            "git ls-remote": cp(0, out=_HEAD),
            "gh pr list": cp(
                0, out='[{"number": 41, "headRefName": "docs-refresh/2026-09-04-1800"}]'
            ),
        }
    )
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_NO_PR
    assert out.branch == "docs-refresh/2026-09-03-0600"
    assert "NO open PR" in out.message


def test_an_origin_that_never_answers_is_bounded_and_fails_closed():
    """`lib.git.git` has no default timeout and this runs under the git-tree lock.

    An unbounded ls-remote against a blackholed origin parks the GitOps deployer, which waits
    `flock -w 180`. A raise here would also reach Kuma as a flattened Python traceback.
    """
    rec = Recorder(
        {"git ls-remote": subprocess.TimeoutExpired(cmd=["git"], timeout=30.0)}
    )
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_ORIGIN_UNREADABLE
    assert "did not answer within 30s" in out.message
    assert not any(c[0] == "gh" for c in rec.calls)


def test_the_pre_flight_bounds_its_own_ls_remote():
    """The bound must reach the transport, not just exist as a constant."""
    seen: dict[str, object] = {}

    def git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen[args[0]] = kwargs.get("timeout")
        return cp()

    publish_pr.unlanded(
        "t/",
        publish_pr.PublishTools(
            git=git, gh=lambda *a, **kw: cp(), land=lambda *a, **kw: cp()
        ),
    )
    assert seen["ls-remote"] == publish_pr.LS_REMOTE_TIMEOUT_S


def test_a_pr_lookup_that_times_out_cannot_clear_the_branch():
    """ls-remote decides; the PR number only labels. A `gh` failure must not read as clean."""
    rec = Recorder({"git ls-remote": cp(0, out=_HEAD), "gh pr list": TIMEOUT})
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_NO_PR
    assert "did not answer" in out.message


def test_the_clean_path_spends_no_gh_call():
    """The quota that makes `gh` time out is 60/hour and shared per host.

    ls-remote goes over git's own credential path, so the common case costs nothing from it.
    """
    rec = Recorder({"git ls-remote": cp(0, out="")})
    _unlanded(rec)
    assert not any(c[0] == "gh" for c in rec.calls)
