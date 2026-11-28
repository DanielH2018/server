"""The merge wait backs off a rate-limited read until the quota frees, holding no turn.

The merge loop's CI read runs the real `await_ci_verdict` and `await_ci.wait`; only GitHub's
check-runs and check-suites reads are replaced, by a 403 that names when the quota frees.
The tests of the landing turn run the merge wait in `turn.held`, as the pipeline does.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_merge_rate_limit.py
"""

import email.message
import io
import subprocess
import urllib.error
from dataclasses import replace
from functools import partial

import pytest

from _land_fakes import Fakes
from _land_turn_fakes import MASTER_MOVED, MERGED, OPEN, land_in_turn, turn_landing
from deploy_tools import await_ci
from deploy_tools.land_lib import merge, tools
from deploy_tools.land_lib.outcome import Outcome
from lib import gh_rate_limit

QUOTA_FREES_IN_S = 900
MERGE_POLL_S = 30
_HEAD = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "c592b53c"}
_MERGED = {"state": "MERGED", "mergeable": "UNKNOWN", "headRefOid": "c592b53c"}


def _rate_limited(spent: bool = False) -> urllib.error.HTTPError:
    """A secondary limit's 403, or the spent core quota's, which reports a reset far ahead."""
    headers = email.message.Message()
    if spent:
        headers["X-RateLimit-Remaining"] = "0"
        headers["X-RateLimit-Reset"] = "4000000000"
        headers["X-RateLimit-Resource"] = "core"
        body = b'{"message": "API rate limit exceeded for user ID 41801780."}'
    else:
        headers["Retry-After"] = str(QUOTA_FREES_IN_S)
        body = b'{"message": "You have exceeded a secondary rate limit."}'
    return urllib.error.HTTPError(
        "https://api.github.com/x",
        403,
        "rate limit exceeded",
        headers,
        io.BytesIO(body),
    )


@pytest.fixture
def ci_reads():
    """The SHAs GitHub refused a CI read of for its rate; each refusal is closed after."""
    reads = []
    yield reads
    for _sha, error in reads:
        error.close()


def _rate_limited_landing(landing, ci_reads, views, merge_timeout, spent=False):
    """A landing whose real CI read goes to a GitHub that refuses every read for its rate."""
    ln, calls = landing(
        Fakes(gh_views={"state,mergeable,headRefOid": views}),
        merge_timeout=merge_timeout,
        merge_poll=MERGE_POLL_S,
    )

    def refuse(sha):
        ci_reads.append((sha, _rate_limited(spent)))
        raise ci_reads[-1][1]

    wait = partial(
        await_ci.wait,
        sleep=ln.tools.sleep,
        clock=ln.tools.clock,
        fetch=refuse,
        fetch_suites=refuse,
    )
    ln.tools = replace(
        ln.tools, await_ci=partial(tools.await_ci_verdict, wait=wait, token=lambda: "t")
    )
    return ln, calls


def _sleeps(calls) -> list[int]:
    return [c[1][0] for c in calls if c[0] == "sleep"]


def test_a_rate_limited_ci_read_sleeps_until_the_quota_frees(landing, ci_reads):
    ln, calls = _rate_limited_landing(
        landing, ci_reads, [_HEAD, _MERGED], merge_timeout=7200
    )
    merge.await_merge(ln)
    assert ci_reads, "the merge loop never read the head's CI"
    [slept] = _sleeps(calls)
    assert QUOTA_FREES_IN_S <= slept <= QUOTA_FREES_IN_S + MERGE_POLL_S


def test_a_spent_quota_that_resets_early_ends_the_sleep(landing, ci_reads):
    # The fake `gh api rate_limit` answers the core quota with every call left.
    ln, calls = _rate_limited_landing(
        landing, ci_reads, [_HEAD, _MERGED], merge_timeout=7200, spent=True
    )
    merge.await_merge(ln)
    assert ci_reads, "the merge loop never read the head's CI"
    assert _sleeps(calls) == [gh_rate_limit.RECHECK_S]


def test_merge_timeout_still_bounds_a_rate_limited_wait(landing, ci_reads):
    ln, calls = _rate_limited_landing(
        landing, ci_reads, [_HEAD], merge_timeout=2 * QUOTA_FREES_IN_S
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    slept = _sleeps(calls)
    assert 2 * QUOTA_FREES_IN_S <= sum(slept) <= 3 * QUOTA_FREES_IN_S
    # One CI read per freed quota, not one per merge poll.
    assert len(ci_reads) <= 3


def test_a_rate_limited_merge_timeout_names_the_rate_limit(landing, ci_reads):
    ln, _calls = _rate_limited_landing(
        landing, ci_reads, [_HEAD], merge_timeout=2 * QUOTA_FREES_IN_S
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert "refused its CI read for the rate" in exc.value.detail
    assert "gh run rerun" not in exc.value.detail


def _turn_order(calls) -> list[str]:
    """The turn's takes and releases, the sleeps and every write to the PR, in order."""
    order = []
    for name, args, _ in calls:
        if name in ("try_turn", "release_turn", "sleep"):
            order.append(name)
        elif name == "gh" and args[:3] == ("pr", "ready", "--undo"):
            order.append("draft")
        elif name == "gh" and args[:2] == ("pr", "ready"):
            order.append("ready")
        elif name == "gh" and "PUT" in args:
            order.append("update" if args[3].endswith("/update-branch") else "merge")
    return order


SECONDARY_LIMIT = subprocess.CalledProcessError(
    1, "gh", stderr="gh: You have exceeded a secondary rate limit. (HTTP 403)"
)


def _refuse_once(ln, field: str) -> None:
    """Refuse the first `gh pr view --json <field>` read for the rate."""
    real, refused = ln.tools.gh_json, []

    def gh_json(*args, **kwargs):
        if args[-1] == field and not refused:
            refused.append(args)
            raise SECONDARY_LIMIT
        return real(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=gh_json)


def test_a_read_refused_inside_the_turn_gives_the_turn_back_for_its_wait(landing):
    # The first read inside the draft's turn is the compare of its base with master.
    ln, calls = turn_landing(landing, compare=[SECONDARY_LIMIT, []])
    land_in_turn(ln)
    assert _turn_order(calls) == [
        "try_turn",
        "release_turn",
        "sleep",
        "try_turn",
        "ready",
        "merge",
        "release_turn",
    ]


def test_a_ci_read_refused_inside_the_turn_gives_the_turn_back_for_its_wait(landing):
    ln, calls = turn_landing(landing, polls=(OPEN, OPEN, MERGED))
    verdicts = iter([await_ci.RateLimitedError(MERGE_POLL_S)])

    def ci(sha, timeout, on_checks):
        refusal = next(verdicts, None)
        if refusal:
            raise refusal
        return tools.CiVerdict(0, "CI green")

    ln.tools = replace(ln.tools, await_ci=ci)
    land_in_turn(ln)
    assert _turn_order(calls) == [
        "try_turn",
        "ready",
        "release_turn",
        "sleep",
        "try_turn",
        "merge",
        "release_turn",
    ]


def test_a_head_master_moved_past_during_the_wait_rejoins_the_line_unmerged(landing):
    ln, calls = turn_landing(landing, compare=[[], MASTER_MOVED[0]])
    _refuse_once(ln, "commits")
    land_in_turn(ln)
    assert _turn_order(calls) == [
        "try_turn",
        "ready",
        "release_turn",
        "sleep",
        "try_turn",
        "draft",
        "update",
        "release_turn",
    ]
