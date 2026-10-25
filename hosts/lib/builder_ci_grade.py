"""Grade a commit's CI check runs the way the deployer's gate does, for the builder.

Split from host_package_builder.py (H3.12), which imports it. The
builder cannot import `deploy_git`, so this module carries its own copy of `ci_verdict`, the two
conclusion sets and `read_check_pages`, and a test holds the copy equal to the deployer's.
Stdlib only, because the builder ships beside it with nothing else.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Callable

# deploy_git.py's two conclusion sets, which `ci_verdict` below grades by. A test holds the
# two copies equal, because this script cannot import the deployer's module.
_CI_PASS_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
_CI_NO_VERDICT_CONCLUSIONS = frozenset(
    {"cancelled", "stale", "skipped_by_concurrency", None}
)


# deploy_git.py's page size and page cap for a check-runs listing, which `read_check_pages`
# below walks by. The same test holds these equal to the deployer's.
_CHECK_PAGE_SIZE, _CHECK_PAGES_MAX = 100, 20


def ci_verdict(check_runs: list[dict], required: frozenset[str]) -> str:
    """`pass`, `pending` or `fail` for one commit, graded as `deploy_git.ci_verdict` grades.

    One difference: the caller refuses an empty `required` before it asks (builder step 4),
    so this never meets the empty set that disarms the deployer's gate.
    """
    states: dict[str, set[str]] = {}
    for run in check_runs:
        name = run.get("name")
        if name not in required:
            continue
        if run.get("status") != "completed":
            state = "pending"
        elif run.get("conclusion") in _CI_PASS_CONCLUSIONS:
            state = "pass"
        elif run.get("conclusion") in _CI_NO_VERDICT_CONCLUSIONS:
            state = "pending"
        else:
            state = "fail"
        states.setdefault(name, set()).add(state)
    if any("fail" in s for s in states.values()):
        return "fail"
    if any("pending" in states.get(name, {"pending"}) for name in required):
        return "pending"
    return "pass"


def read_check_pages(fetch_page: Callable[[str], object], key: str) -> list[dict]:
    """Every entry of `key` across a check-runs listing, walked as `deploy_git.read_check_pages`.

    GitHub pages the listing at 100 runs, so a failing re-run of a required check can sit on
    page 2 only (#5991). `fetch_page(query)` returns the parsed body for the `?per_page=&page=`
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
