"""The CI settings and the paged check-runs read that the builder and checkout-sync share.

Split from host_package_builder.py, which imports it. The grading itself is
`ci_verdict.ci_verdict`, which both components ship beside this module. Stdlib only, because
the builder ships beside it with nothing else. checkout_sync.py imports `CiSettings`,
`read_check_pages` and `token_refused`, and the `checkout-sync` component ships it too.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass


# The page size and page cap for a check-runs listing, which `read_check_pages` below walks
# by. renovate-notify's `notify_logic` holds a copy, and its test holds the two equal.
_CHECK_PAGE_SIZE, _CHECK_PAGES_MAX = 100, 20


@dataclass(frozen=True)
class CiSettings:
    """The CI gate a component grades a commit by, parsed from its settings file.

    Attributes:
        contexts: the check-run names that must be green; never empty.
        walk_max: the most commits one run grades, the tip included; always positive.
    """

    contexts: frozenset[str]
    walk_max: int

    @classmethod
    def parse(cls, env: Mapping[str, str]) -> CiSettings:
        """`CI_CONTEXTS`, a comma-separated list, and `CI_ANCESTOR_WALK_MAX` from `env`.

        Raises:
            ValueError: the context list is empty, which `ci_verdict` would read as a disarmed
                gate that grades every commit green, or the walk bound is not a positive integer.
        """
        contexts = frozenset(
            c.strip() for c in env.get("CI_CONTEXTS", "").split(",") if c.strip()
        )
        walk = env.get("CI_ANCESTOR_WALK_MAX", "").strip()
        if not contexts:
            raise ValueError("CI_CONTEXTS is empty")
        if not walk.isdigit() or int(walk) <= 0:
            raise ValueError(f"CI_ANCESTOR_WALK_MAX={walk!r} is not a positive integer")
        return cls(contexts, int(walk))


def read_check_pages(fetch_page: Callable[[str], object], key: str) -> list[dict]:
    """Every entry of `key` across a paged check-runs listing, up to `_CHECK_PAGES_MAX` pages.

    GitHub pages the listing at 100 runs, so a failing re-run of a required check can sit on
    page 2 only. `fetch_page(query)` returns the parsed body for the `?per_page=&page=`
    string this walk builds. A read short of `total_count` raises rather than returning a
    partial list, and the caller reads that as `pending`.

    Raises:
        ValueError: a body without a `key` list or an integer `total_count`, a short read, or
            more than _CHECK_PAGES_MAX pages.
    """
    found: list[dict] = []
    for page in range(1, _CHECK_PAGES_MAX + 1):
        body = fetch_page(f"?per_page={_CHECK_PAGE_SIZE}&page={page}")
        entries = body.get(key) if isinstance(body, dict) else None
        total = body.get("total_count") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            raise ValueError(f"{key} page {page}: the body has no {key} list")
        if not isinstance(total, int) or isinstance(total, bool):
            raise ValueError(f"{key} page {page}: the body has no total_count")
        found.extend(entries)
        if len(entries) < _CHECK_PAGE_SIZE or len(found) >= total:
            if len(found) < total:
                raise ValueError(f"{key}: read {len(found)} of {total}")
            return found
    raise ValueError(f"{key}: more than {_CHECK_PAGES_MAX} pages")


def token_refused(exc: urllib.error.HTTPError) -> bool:
    """Whether GitHub refused the token, rather than rate-limiting it.

    GitHub answers a revoked or expired token 401, and a token without access 403. It also
    answers a rate limit 403: the primary limit with `X-RateLimit-Remaining: 0`, the
    secondary one with `Retry-After`. A rate limit clears on its own, so it stays a pending
    verdict rather than a page.
    """
    if exc.code == 401:
        return True
    headers = exc.headers or {}
    limited = headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in headers
    return exc.code == 403 and not limited
