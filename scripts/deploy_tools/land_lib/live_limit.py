"""What kind of PR a landing carries, and the quota floor, with the GitHub reads both need.

`slots.py` reads both: the kind decides which of its rules bar a merge, and the floor lowers
the count of landing slots. `batch.py` reads the kind to keep a serial PR out of a batch.
Every landing polls GitHub on one shared token, whose core quota is 5000 calls an hour, and
on 2026-11-11 thirteen live landings spent it.

THE READS FAIL OPEN. A read that fails lowers nothing and bars nothing. `gh api rate_limit`
costs no core quota, and the PR's own file list is one GraphQL read.

Run: uv run pytest scripts/deploy_tools/tests/test_land_live_limit.py
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.json_types import JsonValue, as_list, as_object
from lib.role_paths import CLUSTER_VARS_PREFIX, is_test_path, role_for
from validate.cluster_pins import PINS

QUOTA_FLOOR = 1500
QUOTA_RESET_FAR_S = 15 * 60


K8S = "k8s"
SERIAL = "serial"
# The role holding each forward-only pin is named for it: `k3s_longhorn_version` in `longhorn`.
SERIAL_ROLES = frozenset(
    {key.removeprefix("k3s_").removesuffix("_version") for key in PINS} | {"flux"}
)
# DECIDED: every edit to the service list is serial, a field change on one service included.
# Every tag promotes with the bundle the list declares, and one crashlooping new service fails
# that whole promotion and sets `DEPLOY_HOLD`.
SERIAL_PATHS = frozenset({"cluster/services.yml"})
# What a serial PR changes, as the object of "changes" in a refusal.
SERIAL_WHAT = "a forward-only pin, the flux role, cluster-vars or " + " or ".join(
    sorted(SERIAL_PATHS)
)


def kind(paths: Iterable[str]) -> str:
    """`SERIAL`, `K8S`, or "" for a PR changing `paths`."""
    paths = list(paths)
    roles = {role_for(p) for p in paths if not is_test_path(p)}
    if roles & SERIAL_ROLES or any(
        p in SERIAL_PATHS or p.startswith(CLUSTER_VARS_PREFIX) for p in paths
    ):
        return SERIAL
    return K8S if roles - {None} else ""


@dataclass(frozen=True)
class Reads:
    """The boundaries, one field each, so a test replaces one.

    Attributes:
        pr_paths: the paths a PR changes.
        core_quota: the core quota's `(remaining, reset epoch)`.
        now: the time the quota floor reads.
    """

    pr_paths: Callable[[str], list[str]]
    core_quota: Callable[[], tuple[int, int]]
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def github_reads(gh_json: Callable[..., JsonValue]) -> Reads:
    """`Reads` over `gh_json`, the landing's own `gh` boundary."""

    def pr_paths(pr: str) -> list[str]:
        got = as_object(gh_json("pr", "view", pr, "--json", "files"), "gh pr view")
        return [
            str(as_object(f, "a file")["path"]) for f in as_list(got["files"], "files")
        ]

    def core_quota() -> tuple[int, int]:
        core = as_object(
            gh_json("api", "rate_limit", "--jq", ".resources.core"), "core"
        )
        return int(str(core["remaining"])), int(str(core["reset"]))

    return Reads(pr_paths=pr_paths, core_quota=core_quota)


def fail_open[T](read: Callable[[], T]) -> T | None:
    """`read()`, or None when it raises anything."""
    # DECIDED: any failure of a read lowers nothing and bars nothing. See the module docstring.
    try:
        return read()
    except Exception:
        return None


def low_quota(reads: Reads) -> str | None:
    """Why the core quota lowers the limit to 1, or None while it does not or cannot be read."""
    quota = fail_open(reads.core_quota)
    if not quota:
        return None
    remaining, reset = quota
    resets_in = reset - int(reads.now().timestamp())
    if remaining < QUOTA_FLOOR and resets_in > QUOTA_RESET_FAR_S:
        return f"the core quota has {remaining} calls left and resets in {resets_in // 60}m"
    return None


def pr_kind(pr: str, reads: Reads) -> str:
    """`kind` of `pr`'s file list; "" when the list cannot be read."""
    paths = fail_open(lambda: reads.pr_paths(pr))
    return kind(paths) if paths else ""
