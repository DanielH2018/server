"""A read-only `gh` call retried through a transient failure before the landing refuses (#5503).

From 2026-10-15 to 2026-10-17, one GitHub GraphQL i/o timeout each refused four landings, and
every one was re-run by hand. The verdict was `refused`, which reads as a policy refusal rather
than a network blip. `read` therefore retries a timeout or a 5xx after each wait in
`BACKOFF_S`, three attempts in all. A 4xx, or any answer gh parsed, is GitHub saying no, so it
fails at once.
"""

import re
import subprocess
from collections.abc import Callable
from typing import TypeVar

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.outcome import say

T = TypeVar("T")

# The waits between attempts: three attempts in all.
BACKOFF_S = (5, 15)
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


def read(call: Callable[[], T], sleep: Callable[[float], None], label: str) -> T:
    """`call()`, retried on a transient gh failure; `GhReadFailed` once it is not worth another.

    Only gh's own failures are caught. Anything else `call` raises, such as the `ValueError`
    of an unparseable answer, propagates on the first attempt: gh answered, and asking again
    gets the same answer.
    """
    for attempt, wait in enumerate((*BACKOFF_S, None), start=1):
        try:
            return call()
        except subprocess.CalledProcessError as exc:
            why = (exc.stderr or "").strip()
        except subprocess.TimeoutExpired:
            why = "gh timed out"
        if wait is None or not is_transient(why):
            raise GhReadFailed(why, attempt)
        say(f"{label} failed transiently ({why}); retrying in {wait}s")
        sleep(wait)
    raise AssertionError("unreachable: the last wait is None")
