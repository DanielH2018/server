"""`gh_rate_limit.wait_s` turns a rate-limit refusal into a pause until the quota resets (#7993).

Run: uv run pytest scripts/lib/tests/test_gh_rate_limit.py
"""

import subprocess

import pytest

from lib import gh_rate_limit

PRIMARY = "gh: API rate limit exceeded for user ID 41801780. (HTTP 403)"
SECONDARY = "gh: You have exceeded a secondary rate limit. (HTTP 403)"
NOW = 1_000_000.0


def _run_answering(stdout: str):
    def run(argv, **_kwargs):
        assert argv[:3] == ["gh", "api", "rate_limit"]
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return run


@pytest.mark.parametrize("why", [PRIMARY, SECONDARY])
def test_a_rate_limit_refusal_is_recognised(why):
    assert gh_rate_limit.is_rate_limited(why)


@pytest.mark.parametrize("why", ["HTTP 403: Resource not accessible", "HTTP 502"])
def test_other_refusals_are_not_rate_limits(why):
    assert not gh_rate_limit.is_rate_limited(why)


def test_the_quota_waits_until_its_reset():
    got = gh_rate_limit.wait_s(
        PRIMARY, _run_answering(f"{int(NOW) + 600}\n"), lambda: NOW
    )
    assert got == 602


def test_the_wait_never_exceeds_one_quota_window():
    run = _run_answering(f"{int(NOW) + 10 * 3600}\n")
    assert gh_rate_limit.wait_s(PRIMARY, run, lambda: NOW) == gh_rate_limit.MAX_WAIT_S


def test_an_unreadable_reset_waits_the_fallback():
    assert (
        gh_rate_limit.wait_s(PRIMARY, _run_answering("not a number"), lambda: NOW)
        == gh_rate_limit.UNKNOWN_RESET_WAIT_S
    )


def test_a_secondary_limit_waits_without_reading_the_reset():
    def run(*_args, **_kwargs):
        raise AssertionError("a secondary limit has no reset to read")

    assert gh_rate_limit.wait_s(SECONDARY, run) == gh_rate_limit.SECONDARY_WAIT_S
