"""`gh_rate_limit` turns a rate-limit refusal into a pause, and a refused write into its remedy.

Run: uv run pytest scripts/lib/tests/git_and_gh/test_gh_rate_limit.py
"""

import email.message
import io
import json
import subprocess
import urllib.error
from datetime import UTC, datetime

import pytest

from lib import gh_rate_limit

PRIMARY = "gh: API rate limit exceeded for user ID 41801780. (HTTP 403)"
SECONDARY = "gh: You have exceeded a secondary rate limit. (HTTP 403)"
# The GraphQL quota's spelling carries "already" inside the phrase.
GRAPHQL = "GraphQL: API rate limit already exceeded for user ID 41801780."
NOW = 1_000_000.0


def _run_answering(reset, remaining=0):
    """`gh api rate_limit` answering one quota, as its `--jq` filter asks for it."""

    def run(argv, **_kwargs):
        assert argv[:3] == ["gh", "api", "rate_limit"]
        jq = argv[argv.index("--jq") + 1]
        quota = {"remaining": remaining, "reset": reset}
        out = str(reset) if jq.endswith(".reset") else json.dumps(quota)
        return subprocess.CompletedProcess(argv, 0, out + "\n", "")

    return run


@pytest.mark.parametrize("why", [PRIMARY, SECONDARY, GRAPHQL])
def test_a_rate_limit_refusal_is_recognised(why):
    assert gh_rate_limit.is_rate_limited(why)


@pytest.mark.parametrize("why", ["HTTP 403: Resource not accessible", "HTTP 502"])
def test_other_refusals_are_not_rate_limits(why):
    assert not gh_rate_limit.is_rate_limited(why)


def test_the_quota_waits_until_its_reset():
    got = gh_rate_limit.wait_s(PRIMARY, _run_answering(int(NOW) + 600), lambda: NOW)
    assert got == 602


def test_the_wait_never_exceeds_one_quota_window():
    run = _run_answering(int(NOW) + 10 * 3600)
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
    """GraphQL's quota resets apart from core's, so its 403 waits on its own reset."""
    resets = {"core": int(NOW) + 3000, "graphql": int(NOW) + 100}

    def run(argv, **_kwargs):
        assert argv[:3] == ["gh", "api", "rate_limit"]
        jq = argv[argv.index("--jq") + 1] if "--jq" in argv else ""
        resources = {
            name: {"remaining": 0, "reset": reset} for name, reset in resets.items()
        }
        if jq.endswith(".reset"):
            out = str(resets["graphql" if "graphql" in jq else "core"])
        elif jq.startswith(".resources."):
            out = json.dumps(resources[jq.removeprefix(".resources.")])
        elif jq == ".resources":
            out = json.dumps(resources)
        else:
            out = json.dumps({"resources": resources})
        return subprocess.CompletedProcess(argv, 0, out + "\n", "")

    why = "GraphQL: API rate limit exceeded for user ID 41801780."
    assert gh_rate_limit.wait_s(why, run, lambda: NOW) == 102


def test_a_refusal_in_primary_words_with_quota_left_waits_as_a_secondary_limit():
    run = _run_answering(int(NOW) + 3000, remaining=4595)
    assert gh_rate_limit.wait_s(GRAPHQL, run, lambda: NOW) == (
        gh_rate_limit.SECONDARY_WAIT_S
    )


def _quota_over_time(*answers):
    """A clock that `sleep` advances, and a `gh api rate_limit` giving `answers` in turn.

    Each answer is `(remaining, reset)`; the last one repeats. Returns the seconds slept, in
    a list, then `sleep`, `run` and `now` for `wait_out`.
    """
    clock = [NOW]
    slept: list[float] = []
    queue = list(answers)

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    def run(argv, **_kwargs):
        remaining, reset = queue.pop(0) if len(queue) > 1 else queue[0]
        quota = json.dumps({"remaining": remaining, "reset": reset})
        return subprocess.CompletedProcess(argv, 0, quota + "\n", "")

    return slept, sleep, run, lambda: clock[0]


def test_a_wait_for_a_spent_quota_ends_at_a_reset_sooner_than_the_first():
    slept, sleep, run, now = _quota_over_time((0, int(NOW) + 2689), (0, int(NOW) + 600))
    lines: list[str] = []
    gh_rate_limit.wait_out(PRIMARY, sleep, lines.append, run, now)
    assert lines == [
        "hit the GitHub rate limit; waiting 2691s",
        "stopped waiting after 600s: the core quota has reset",
    ]
    assert sum(slept) == 600


def test_a_wait_for_a_spent_quota_ends_once_the_quota_has_calls_again():
    slept, sleep, run, now = _quota_over_time(
        (0, int(NOW) + 2689), (0, int(NOW) + 2689), (5000, int(NOW) + 3600)
    )
    lines: list[str] = []
    gh_rate_limit.wait_out(GRAPHQL, sleep, lines.append, run, now)
    assert sum(slept) == 2 * gh_rate_limit.RECHECK_S
    assert lines[-1] == "stopped waiting after 120s: the graphql quota has reset"


def test_a_secondary_limit_sleeps_its_whole_wait_without_reading_a_quota():
    slept: list[float] = []

    def read(_name):
        raise AssertionError("a secondary limit spends no quota to read")

    assert gh_rate_limit.sleep_until_reset("", 300, slept.append, read) == 300
    assert slept == [300]


def _quota(remaining, reset=int(NOW) + 600):
    return lambda _name: {"remaining": remaining, "reset": reset}


def test_a_refusal_in_primary_words_with_quota_left_is_named_secondary():
    got = gh_rate_limit.remedy(GRAPHQL, _quota(4595))
    assert got is not None
    assert got.startswith("GitHub's secondary rate limit refused it, with 4595 graphql")
    assert got.endswith("wait a few minutes, then re-run this")


def test_a_spent_quota_names_its_reset():
    reset = int(datetime(2026, 11, 18, 15, 21, tzinfo=UTC).timestamp())
    assert gh_rate_limit.remedy(GRAPHQL, _quota(0, reset)) == (
        "the hourly graphql quota is spent until 15:21 UTC; re-run this after it resets"
    )


def test_a_refusal_naming_the_secondary_limit_reads_no_quota():
    def read(_name):
        raise AssertionError("GitHub already named the limit")

    assert gh_rate_limit.remedy(SECONDARY, read) == (
        "GitHub's secondary rate limit refused it; wait a few minutes, then re-run this"
    )


def test_an_unreadable_quota_still_says_to_wait():
    def read(_name):
        raise subprocess.CalledProcessError(1, ["gh"])

    got = gh_rate_limit.remedy(PRIMARY, read)
    assert (
        got
        == "GitHub refused it for its request rate; wait a few minutes, then re-run this"
    )


def test_a_refusal_that_is_no_rate_limit_has_no_remedy():
    assert gh_rate_limit.remedy("HTTP 403: Resource not accessible", _quota(0)) is None


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
        assert gh_rate_limit.http_wait(error, lambda: NOW) == (602, "core")


def test_a_rest_403_with_retry_after_waits_that_long():
    with _http_403(Retry_After="45") as error:
        assert gh_rate_limit.http_wait(error, lambda: NOW) == (45, "")


def test_a_rest_403_with_quota_left_is_not_a_rate_limit():
    with _http_403(X_RateLimit_Remaining="4999") as error:
        assert gh_rate_limit.http_wait(error, lambda: NOW) is None


def test_a_rest_403_whose_body_names_a_secondary_limit_waits_the_minimum():
    body = b'{"message": "You have exceeded a secondary rate limit."}'
    with _http_403(body, X_RateLimit_Remaining="4999") as error:
        assert gh_rate_limit.http_wait(error, lambda: NOW) == (
            gh_rate_limit.SECONDARY_WAIT_S,
            "",
        )
