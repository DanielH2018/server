"""A read-only `gh` call retried through a transient failure before the landing refuses.

One GitHub GraphQL i/o timeout would otherwise refuse a landing, and the verdict `refused`
reads as a policy refusal rather than a network blip. `read` therefore retries a timeout or a 5xx after each wait in
`BACKOFF_S`, three attempts in all. A 4xx, or any answer gh parsed, is GitHub saying no, so it
fails at once.

A rate-limit 403 is the exception to that: it says nothing about the request, so `read` waits
until the quota resets and asks again, up to `RATE_LIMIT_WAITS` times. The wait reads the
quota as it goes, so it ends at the reset (`lib.gh_rate_limit.wait_out`).
"""

import re
import subprocess
from collections.abc import Callable
from typing import TypeVar


from deploy_tools.land_lib.outcome import say
from lib import gh_rate_limit

T = TypeVar("T")

# The waits between attempts: three attempts in all.
BACKOFF_S = (5, 15)
# The rate-limit waits one read sits through before it gives up.
RATE_LIMIT_WAITS = 3
# What gh prints for a read GitHub never answered: a timeout of its own or a 5xx.
_TRANSIENT = re.compile(r"timed? ?out|HTTP 5\d\d", re.IGNORECASE)


class GhReadFailed(Exception):
    """A gh read that failed for good, with how many attempts it took to give up."""

    def __init__(self, why: str, attempts: int) -> None:
        self.why, self.attempts = why, attempts
        suffix = f" (after {attempts} attempts)" if attempts > 1 else ""
        super().__init__(f"{why}{suffix}")


def is_transient(why: str) -> bool:
    """Is this failed gh read worth retrying -- a timeout or a 5xx, not a 4xx answer?"""
    return bool(_TRANSIENT.search(why))


def read(
    call: Callable[[], T],
    sleep: Callable[[float], None],
    label: str,
    rate_limit_waits: int = RATE_LIMIT_WAITS,
) -> T:
    """`call()`, retried on a transient gh failure; `GhReadFailed` once it is not worth another.

    A rate limit is waited out up to `rate_limit_waits` times. A read made while the landing
    holds the landing turn passes 0, so a rate limit fails it at once instead of holding the
    turn through the wait.

    Only gh's own failures are caught. Anything else `call` raises, such as the `ValueError`
    of an unparseable answer, propagates on the first attempt: gh answered, and asking again
    gets the same answer.
    """
    waited_out = 0
    for attempt, wait in enumerate((*BACKOFF_S, None), start=1):
        while True:
            try:
                return call()
            except subprocess.CalledProcessError as exc:
                why = (exc.stderr or "").strip()
            except subprocess.TimeoutExpired:
                why = "gh timed out"
            if not gh_rate_limit.is_rate_limited(why) or waited_out >= rate_limit_waits:
                break
            waited_out += 1
            gh_rate_limit.wait_out(why, sleep, lambda line: say(f"{label} {line}"))
        if wait is None or not is_transient(why):
            raise GhReadFailed(why, attempt)
        say(f"{label} failed transiently ({why}); retrying in {wait}s")
        sleep(wait)
    raise AssertionError("unreachable: the last wait is None")
