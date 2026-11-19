"""How long a `gh` call that GitHub refused for rate limiting waits, and what its caller says.

A rate-limit 403 is not an answer about the request. The shared token spends 5000 core calls an
hour, and a landing that refused on the 403 was relaunched by hand, which spent more. A caller
that reads through `gh` asks `wait_s` for the pause and retries after it; one that reads the
REST API itself asks `http_wait_s`. A caller that reports a refused write asks `remedy` for
the next step.

A secondary limit can arrive in a primary quota's words: GitHub refused GraphQL writes as
"API rate limit already exceeded" while that quota had 4595 of 5000 calls left. So a refusal
counts as secondary when it says so, or when the quota it names still has calls left.
"""

import json
import re
import subprocess
import time
import urllib.error
from collections.abc import Callable
from datetime import UTC, datetime

# gh prints the API's message: "API rate limit exceeded for user ID N" for the hourly core
# quota, "API rate limit already exceeded for user ID N" for the GraphQL quota, and "You have
# exceeded a secondary rate limit" for a burst.
_RATE_LIMITED = re.compile(
    r"rate limit (?:already )?exceeded|secondary rate limit", re.IGNORECASE
)
_SECONDARY = re.compile(r"secondary rate limit", re.IGNORECASE)
# gh prefixes a GraphQL error with "GraphQL:"; that quota resets apart from core's.
_GRAPHQL = re.compile(r"\bGraphQL\b", re.IGNORECASE)

# gh's stderr carries no `Retry-After`, so a secondary limit waits GitHub's documented minimum.
SECONDARY_WAIT_S = 60
# The wait when the reset time cannot be read; the quota resets on the hour at the latest.
UNKNOWN_RESET_WAIT_S = 300
# No wait exceeds one quota window.
MAX_WAIT_S = 3600
_SLACK_S = 2


def is_rate_limited(why: str) -> bool:
    """Is this gh stderr GitHub refusing the caller for its request rate?"""
    return bool(_RATE_LIMITED.search(why))


def wait_s(
    why: str,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    now: Callable[[], float] = time.time,
) -> int:
    """Seconds to wait before a call that failed with `why` is worth repeating.

    A spent hourly quota waits until its reset, which `gh api rate_limit` reports and GitHub
    does not count against the quota. A GraphQL refusal reads the `graphql` quota, any other
    `core`'s. A secondary limit waits `SECONDARY_WAIT_S`.
    """
    if _SECONDARY.search(why):
        return SECONDARY_WAIT_S
    quota = _quota(why, _reader(run))
    if quota is None:
        return UNKNOWN_RESET_WAIT_S
    _name, remaining, reset = quota
    return SECONDARY_WAIT_S if remaining > 0 else _until(reset, now())


def remedy(why: str, read: Callable[[str], object] | None = None) -> str | None:
    """What to do after GitHub refused a call with `why`; None if it was no rate limit.

    Every remedy is to wait and re-run the caller, never to repeat the refused call by hand,
    because the limit that refused it still holds.

    Args:
        why: gh's stderr for the refused call.
        read: one quota's `.resources.<name>` object from `gh api rate_limit`, by name. The
          default runs gh.
    """
    if not is_rate_limited(why):
        return None
    wait = "wait a few minutes, then re-run this"
    if _SECONDARY.search(why):
        return f"GitHub's secondary rate limit refused it; {wait}"
    quota = _quota(why, read or _reader(subprocess.run))
    if quota is None:
        return f"GitHub refused it for its request rate; {wait}"
    name, remaining, reset = quota
    if remaining > 0:
        return (
            f"GitHub's secondary rate limit refused it, with {remaining} {name} calls left "
            f"in the hourly quota (`docs/github-quota.md`); {wait}"
        )
    at = datetime.fromtimestamp(reset, UTC).strftime("%H:%M")
    return (
        f"the hourly {name} quota is spent until {at} UTC; re-run this after it resets"
    )


def _reader(run: Callable[..., subprocess.CompletedProcess]) -> Callable[[str], object]:
    def read(name: str) -> object:
        proc = run(
            ["gh", "api", "rate_limit", "--jq", f".resources.{name}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(proc.stdout)

    return read


def _quota(why: str, read: Callable[[str], object]) -> tuple[str, int, int] | None:
    """`(name, remaining, reset epoch)` of the quota `why` names; None when unreadable."""
    name = "graphql" if _GRAPHQL.search(why) else "core"
    try:
        got = read(name)
        if not isinstance(got, dict):
            return None
        return name, int(got["remaining"]), int(got["reset"])
    except OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError:
        return None


def http_wait_s(
    error: urllib.error.HTTPError, now: Callable[[], float] = time.time
) -> int | None:
    """Seconds to wait before a REST read GitHub refused with `error`; None if no rate limit.

    A spent quota answers 403 or 429 with `X-RateLimit-Remaining: 0` and waits until
    `X-RateLimit-Reset`. A secondary limit waits its `Retry-After`, or `SECONDARY_WAIT_S`.
    A 403 that matches neither header is a rate limit only when its body says so, so this
    reads the body, and a caller cannot read it again.
    """
    if error.code not in (403, 429):
        return None
    headers = error.headers or {}
    retry_after = headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return max(1, min(MAX_WAIT_S, int(retry_after)))
    if headers.get("X-RateLimit-Remaining") == "0":
        reset = headers.get("X-RateLimit-Reset", "")
        return _until(int(reset), now()) if reset.isdigit() else UNKNOWN_RESET_WAIT_S
    if error.code == 429 or is_rate_limited(_body(error)):
        return SECONDARY_WAIT_S
    return None


def _body(error: urllib.error.HTTPError) -> str:
    """The error's response body, which carries GitHub's message; '' if unreadable."""
    try:
        return error.read().decode("utf-8", "replace")
    except OSError, ValueError:
        return ""


def _until(reset: int, now: float) -> int:
    return max(1, min(MAX_WAIT_S, int(reset - now) + _SLACK_S))
