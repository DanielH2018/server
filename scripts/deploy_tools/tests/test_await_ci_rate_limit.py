"""await_ci waits out a rate-limit 403 rather than reading it as pending (#8139).

Run: uv run pytest scripts/deploy_tools/tests/test_await_ci_rate_limit.py
"""

import email.message
import io
import urllib.error

import await_ci
import pytest

INTERVAL_S = 20


@pytest.fixture
def rate_limited():
    """GitHub's 403 for a spent core quota, closed after the test."""
    headers = email.message.Message()
    headers["X-RateLimit-Limit"] = "5000"
    headers["X-RateLimit-Remaining"] = "0"
    # A reset far past any clock: the wait is capped, never shortened to one poll.
    headers["X-RateLimit-Reset"] = "4000000000"
    headers["X-RateLimit-Resource"] = "core"
    body = b'{"message": "API rate limit exceeded for user ID 41801780."}'
    error = urllib.error.HTTPError(
        "https://api.github.com/x",
        403,
        "rate limit exceeded",
        headers,
        io.BytesIO(body),
    )
    yield error
    error.close()


def test_a_rate_limit_403_waits_for_the_reset_rather_than_polling_as_pending(
    rate_limited,
):
    green = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in await_ci.required_contexts()
    ]
    answers = iter([rate_limited])

    def fetch(_sha):
        answer = next(answers, green)
        if isinstance(answer, Exception):
            raise answer
        return answer

    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    code, _msg = await_ci.wait(
        "abc",
        7200,
        INTERVAL_S,
        sleep=sleep,
        clock=lambda: now[0],
        fetch=fetch,
        fetch_suites=lambda _s: [],
    )
    assert code == 0
    assert slept and slept[0] > INTERVAL_S


def test_a_rate_limit_403_is_not_read_as_a_pending_verdict(rate_limited):
    """The descendant walk reads each commit through `verdict_for`'s own catch."""

    def fetch(_sha):
        raise rate_limited

    try:
        got = await_ci.verdict_for("abc", fetch, await_ci.required_contexts())
    except Exception:
        return
    assert got != "pending"


def test_verdict_for_raises_the_rate_limit_that_wait_sleeps_on(rate_limited):
    def fetch(_sha):
        raise rate_limited

    with pytest.raises(await_ci.RateLimitedError):
        await_ci.verdict_for("abc", fetch, await_ci.required_contexts())
