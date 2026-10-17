"""`Landing.view` retries a transient `gh` read failure before it refuses the landing (#5503).

Four landings were refused from 2026-10-15 to 2026-10-17 on one GraphQL i/o timeout each. A
timeout or a 5xx is retried, three attempts in all, with a 5s and then a 15s backoff. A 4xx is
an answer, so the landing refuses on it at once.

Run: uv run pytest scripts/deploy_tools/tests/test_land_view_retries.py
"""

import subprocess

import pytest

from deploy_tools.land_lib.outcome import Outcome

# What gh exits with when GitHub's GraphQL endpoint does not answer: the shape #5296 logged.
IO_TIMEOUT = subprocess.CalledProcessError(
    1,
    "gh",
    stderr='Post "https://api.github.com/graphql": read tcp 10.0.0.2:51234->'
    "140.82.112.6:443: i/o timeout",
)
# gh's own subprocess timeout in `lib.gh`, which `view` reports as "gh timed out".
SUBPROCESS_TIMEOUT = subprocess.TimeoutExpired("gh", 60)
SERVER_ERROR = subprocess.CalledProcessError(1, "gh", stderr="HTTP 502: Bad Gateway")
NOT_FOUND = subprocess.CalledProcessError(
    1, "gh", stderr="HTTP 404: Not Found (https://api.github.com/graphql)"
)
ANSWER = {"headRefOid": "a" * 40}


def _reads(ln, outcomes: list) -> list:
    """Point `gh_json` at `outcomes` in order: an exception is raised, anything else returned.

    Returns the list of argv tuples gh was called with, so a test can count the attempts.
    """
    attempts: list = []
    queue = list(outcomes)

    def gh_json(*args, **kwargs):
        attempts.append(args)
        nxt = queue.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    ln.tools.gh_json = gh_json
    return attempts


def _view_or_fail(ln) -> dict:
    """`ln.view`, with a refusal reported as this test's failure rather than as an error."""
    try:
        return dict(ln.view("headRefOid"))
    except Outcome as exc:
        pytest.fail(f"the landing refused on a transient read: {exc.error}")


def _sleeps(calls: list) -> list:
    return [c[1][0] for c in calls if c[0] == "sleep"]


@pytest.mark.parametrize(
    "transient",
    [IO_TIMEOUT, SUBPROCESS_TIMEOUT],
    ids=["graphql-io-timeout", "gh-subprocess-timeout"],
)
def test_a_read_that_times_out_twice_then_answers_continues_the_landing(
    landing, transient
):
    ln, _ = landing()
    attempts = _reads(ln, [transient, transient, ANSWER])
    assert _view_or_fail(ln) == ANSWER
    assert len(attempts) == 3


def test_a_read_that_times_out_three_times_refuses_and_names_the_timeout(landing):
    ln, _ = landing()
    attempts = _reads(ln, [IO_TIMEOUT, IO_TIMEOUT, IO_TIMEOUT])
    with pytest.raises(Outcome) as exc:
        ln.view("headRefOid")
    assert len(attempts) == 3
    assert exc.value.rc == 1
    assert "timeout" in exc.value.error
    # The line says the read failed after its retries, so it does not read as a policy refusal.
    assert "retr" in exc.value.error or "3 attempts" in exc.value.error


def test_a_read_that_hits_a_404_after_a_timeout_refuses_without_a_third_attempt(
    landing,
):
    """A 404 is GitHub answering, so the retry loop stops on it at once.

    A plain 404 on the first read already refuses at once today; the timeout in front is what
    makes this a check on the retry loop rather than on the code that has no loop.
    """
    ln, calls = landing()
    attempts = _reads(ln, [IO_TIMEOUT, NOT_FOUND, ANSWER])
    with pytest.raises(Outcome) as exc:
        ln.view("headRefOid")
    assert len(attempts) == 2
    assert exc.value.rc == 1 and "HTTP 404" in exc.value.error
    assert _sleeps(calls) == [5]


def test_a_5xx_is_retried_like_a_timeout(landing):
    ln, _ = landing()
    attempts = _reads(ln, [SERVER_ERROR, SERVER_ERROR, ANSWER])
    assert _view_or_fail(ln) == ANSWER
    assert len(attempts) == 3


def test_the_retries_back_off_5s_then_15s(landing):
    ln, calls = landing()
    _reads(ln, [IO_TIMEOUT, IO_TIMEOUT, ANSWER])
    # Whether the read ends in an answer is the first test's claim; this one reads the waits.
    try:
        ln.view("headRefOid")
    except Outcome:
        pass
    assert _sleeps(calls) == [5, 15]
