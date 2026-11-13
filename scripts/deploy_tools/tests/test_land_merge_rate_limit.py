"""The merge wait backs off a rate-limited CI read until the quota frees (#8147).

The merge loop's CI read runs the real `await_ci_verdict` and `await_ci.wait`; only GitHub's
check-runs and check-suites reads are replaced, by a 403 that names when the quota frees.

Run: uv run pytest scripts/deploy_tools/tests/test_land_merge_rate_limit.py
"""

import email.message
import io
import urllib.error
from dataclasses import replace
from functools import partial

import pytest

from _land_fakes import Fakes
from deploy_tools import await_ci
from deploy_tools.land_lib import merge, tools
from deploy_tools.land_lib.outcome import Outcome

QUOTA_FREES_IN_S = 900
MERGE_POLL_S = 30
_HEAD = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "c592b53c"}
_MERGED = {"state": "MERGED", "mergeable": "UNKNOWN", "headRefOid": "c592b53c"}


def _rate_limited() -> urllib.error.HTTPError:
    headers = email.message.Message()
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


def _rate_limited_landing(landing, ci_reads, views, merge_timeout):
    """A landing whose real CI read goes to a GitHub that refuses every read for its rate."""
    ln, calls = landing(
        Fakes(gh_views={"state,mergeable,headRefOid": views}),
        merge_timeout=merge_timeout,
        merge_poll=MERGE_POLL_S,
    )

    def refuse(sha):
        ci_reads.append((sha, _rate_limited()))
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
    assert f"the quota frees in {QUOTA_FREES_IN_S}s" in exc.value.detail
