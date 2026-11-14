"""`gh_rate_limit.wait_s` turns a rate-limit refusal into a pause until the quota resets (#7993).

Run: uv run pytest scripts/lib/tests/test_gh_rate_limit.py
"""

import email.message
import io
import json
import subprocess
import urllib.error

import pytest

from lib import gh_rate_limit

PRIMARY = "gh: API rate limit exceeded for user ID 41801780. (HTTP 403)"
SECONDARY = "gh: You have exceeded a secondary rate limit. (HTTP 403)"
# The GraphQL quota's spelling carries "already" inside the phrase (#8708).
GRAPHQL = "GraphQL: API rate limit already exceeded for user ID 41801780."
NOW = 1_000_000.0


def _run_answering(stdout: str):
    def run(argv, **_kwargs):
        assert argv[:3] == ["gh", "api", "rate_limit"]
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return run


@pytest.mark.parametrize("why", [PRIMARY, SECONDARY, GRAPHQL])
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


def test_a_graphql_refusal_waits_for_the_graphql_reset():
    """GraphQL's quota resets apart from core's, so its 403 waits on its own reset (#8139)."""
    resets = {"core": int(NOW) + 3000, "graphql": int(NOW) + 100}

    def run(argv, **_kwargs):
        assert argv[:3] == ["gh", "api", "rate_limit"]
        jq = argv[argv.index("--jq") + 1] if "--jq" in argv else ""
        resources = {name: {"reset": reset} for name, reset in resets.items()}
        if jq.endswith(".reset"):
            out = str(resets["graphql" if "graphql" in jq else "core"])
        elif jq == ".resources":
            out = json.dumps(resources)
        else:
            out = json.dumps({"resources": resources})
        return subprocess.CompletedProcess(argv, 0, out + "\n", "")

    why = "GraphQL: API rate limit exceeded for user ID 41801780."
    assert gh_rate_limit.wait_s(why, run, lambda: NOW) == 102


def _http_403(body=b"", **headers):
    """A REST 403 carrying `body` and `headers`; a context manager, which closes its body."""
    message = email.message.Message()
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    return urllib.error.HTTPError(
        "https://api.github.com/x", 403, "Forbidden", message, io.BytesIO(body)
    )


def test_a_rest_403_with_the_quota_spent_waits_until_its_reset():
    reset = str(int(NOW) + 600)
    with _http_403(X_RateLimit_Remaining="0", X_RateLimit_Reset=reset) as error:
        assert gh_rate_limit.http_wait_s(error, lambda: NOW) == 602


def test_a_rest_403_with_retry_after_waits_that_long():
    with _http_403(Retry_After="45") as error:
        assert gh_rate_limit.http_wait_s(error, lambda: NOW) == 45


def test_a_rest_403_with_quota_left_is_not_a_rate_limit():
    with _http_403(X_RateLimit_Remaining="4999") as error:
        assert gh_rate_limit.http_wait_s(error, lambda: NOW) is None


def test_a_rest_403_whose_body_names_a_secondary_limit_waits_the_minimum():
    body = b'{"message": "You have exceeded a secondary rate limit."}'
    with _http_403(body, X_RateLimit_Remaining="4999") as error:
        assert gh_rate_limit.http_wait_s(error, lambda: NOW) == (
            gh_rate_limit.SECONDARY_WAIT_S
        )
