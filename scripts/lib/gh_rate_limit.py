"""How long a `gh` call that GitHub refused for rate limiting has to wait (#7993).

A rate-limit 403 is not an answer about the request. The shared token spends 5000 core calls an
hour, and a landing that refused on the 403 was relaunched by hand, which spent more. A caller
that reads through `gh` asks `wait_s` for the pause and retries after it.
"""

import re
import subprocess
import time
from collections.abc import Callable

# gh prints the API's message: "API rate limit exceeded for user ID N" for the hourly quota,
# and "You have exceeded a secondary rate limit" for a burst.
_RATE_LIMITED = re.compile(r"rate limit exceeded|secondary rate limit", re.IGNORECASE)
_SECONDARY = re.compile(r"secondary rate limit", re.IGNORECASE)

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

    The hourly quota waits until `X-RateLimit-Reset`, which `gh api rate_limit` reports and
    GitHub does not count against the quota. A secondary limit waits `SECONDARY_WAIT_S`.
    """
    if _SECONDARY.search(why):
        return SECONDARY_WAIT_S
    try:
        proc = run(
            ["gh", "api", "rate_limit", "--jq", ".resources.core.reset"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        reset = int(proc.stdout.strip())
    except OSError, subprocess.TimeoutExpired, ValueError:
        return UNKNOWN_RESET_WAIT_S
    return max(1, min(MAX_WAIT_S, int(reset - now()) + _SLACK_S))
