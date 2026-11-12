"""await_ci waits out a rate-limit 403 rather than reading it as pending (#8139).

Run: uv run pytest scripts/deploy_tools/tests/test_await_ci_rate_limit.py
"""

import email.message
import io
import urllib.error

import await_ci

INTERVAL_S = 20


def _rate_limited() -> urllib.error.HTTPError:
    headers = email.message.Message()
    headers["X-RateLimit-Limit"] = "5000"
    headers["X-RateLimit-Remaining"] = "0"
    # A reset far past any clock: the wait is capped, never shortened to one poll.
    headers["X-RateLimit-Reset"] = "4000000000"
    headers["X-RateLimit-Resource"] = "core"
    body = b'{"message": "API rate limit exceeded for user ID 41801780."}'
    return urllib.error.HTTPError(
        "https://api.github.com/x",
        403,
        "rate limit exceeded",
        headers,
        io.BytesIO(body),
    )


def test_a_rate_limit_403_waits_for_the_reset_rather_than_polling_as_pending():
    green = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in await_ci.required_contexts()
    ]
    answers = iter([_rate_limited()])

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
