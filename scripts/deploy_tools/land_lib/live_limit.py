"""How many landings one group may run at once, and the condition that lowered it (#8142).

`detach.py` owns the registry of live landings; this module owns the number a new landing is
admitted under. Every landing polls GitHub on one shared token, whose core quota is 5000
calls an hour, and on 2026-11-11 thirteen live landings spent it (#8018). The operator set the
limit to 3 on 2026-11-12. `LAND_MAX_LIVE` overrides it, and the conditions below can only
lower what the override sets:

- to 1 while the core quota has fewer than `QUOTA_FLOOR` calls left and resets more than
  `QUOTA_RESET_FAR_S` away;
- to 2 while `SLOW_RUNS` or more master `ci.yml` runs have been unfinished for more than
  `SLOW_RUN_AGE_S`;
- to 0 for a PR that changes a k8s role while `DEPLOY_HOLD` holds, as the pin gate reads it;
- to 1 for a PR that changes a k8s role inside the weekly reboot's no-promotion window;
- to 1 while a landing of a serial PR is live, or when this PR is one. A serial PR changes the
  role that holds a forward-only cluster pin, the `flux` role, or the cluster-vars file.

THE READS FAIL OPEN. The limit is a throughput control, as merge pacing is: a read that fails
lowers nothing. The landing still meets `DEPLOY_HOLD` at the pin gate and at the promotion.
`gh api rate_limit` costs no core quota, and the PR's own file list is one GraphQL read.

Run: uv run pytest scripts/deploy_tools/tests/test_land_live_limit.py
"""

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from chained_timeout import VERIFY_CAP_S
from lib.json_types import JsonValue, as_list, as_object
from lib.repo_paths import REPO
from lib.role_paths import is_test_path, role_for
from validate.cluster_pins import PINS

from deploy_tools.land_lib import pace, pin_gate, pin_reads
from deploy_tools.promote_lib import reboot_window

MAX_LIVE_ENV = "LAND_MAX_LIVE"
# DECIDED: one number, 3, with no separate hard ceiling. The operator set 3 per token on
# 2026-11-12 (#8018), and a second, higher ceiling would be a number nothing reads.
DEFAULT_LIMIT = 3
QUOTA_FLOOR = 1500
QUOTA_RESET_FAR_S = 15 * 60
SLOW_RUNS = 2
SLOW_RUN_AGE_S = 15 * 60
# DECIDED: 2, the top of the report's 1-2. Pacing already holds each merge while master CI
# has a backlog, so a slow master costs waiting time, not quota.
SLOW_RUNS_LIMIT = 2

K8S = "k8s"
SERIAL = "serial"
# The role holding each forward-only pin is named for it: `k3s_longhorn_version` in `longhorn`.
SERIAL_ROLES = frozenset(
    {key.removeprefix("k3s_").removesuffix("_version") for key in PINS} | {"flux"}
)
CLUSTER_VARS_PREFIX = "ansible/vars/cluster-vars."


def kind(paths: Iterable[str]) -> str:
    """`SERIAL`, `K8S`, or "" for a PR changing `paths`; what its slot records."""
    roles = {role_for(p) for p in paths if not is_test_path(p)}
    if roles & SERIAL_ROLES or any(p.startswith(CLUSTER_VARS_PREFIX) for p in paths):
        return SERIAL
    return K8S if roles - {None} else ""


@dataclass(frozen=True)
class Reads:
    """The limit's boundaries, one field each, so a test replaces one.

    Attributes:
        pr_paths: the paths a PR changes.
        core_quota: the core quota's `(remaining, reset epoch)`.
        master_runs: `pace.RUNS_JQ`'s answer for master's `ci.yml` push runs.
        hold: DEPLOY_HOLD's value, or None when the variable does not exist.
        in_window: whether a time is inside the weekly reboot's no-promotion window.
        now: the time every condition reads.
    """

    pr_paths: Callable[[str], list[str]]
    core_quota: Callable[[], tuple[int, int]]
    master_runs: Callable[[], JsonValue]
    hold: Callable[[], str | None] = field(
        default_factory=lambda: pin_reads.PinReads().hold
    )
    in_window: Callable[[datetime], bool] = lambda now: (
        reboot_window.window(reboot_window.read_schedule(REPO), VERIFY_CAP_S, now)
        is not None
    )
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

    return Reads(
        pr_paths=pr_paths,
        core_quota=core_quota,
        master_runs=lambda: gh_json("api", pace.RUNS_URL, "--jq", pace.RUNS_JQ),
    )


def base() -> tuple[int, str]:
    """`LAND_MAX_LIVE` when it is a positive integer, else `DEFAULT_LIMIT`; and which it was."""
    try:
        return max(
            1, int(os.environ[MAX_LIVE_ENV])
        ), f"{MAX_LIVE_ENV}={os.environ[MAX_LIVE_ENV]}"
    except KeyError, ValueError:
        return DEFAULT_LIMIT, f"the default of {DEFAULT_LIMIT}"


def _read[T](read: Callable[[], T]) -> T | None:
    # DECIDED: any failure of a read lowers nothing. See the module docstring.
    try:
        return read()
    except Exception:
        return None


@dataclass(frozen=True)
class Admission:
    """The PR a new landing carries, and the limit it is admitted under.

    Attributes:
        kind: what its slot records, from `kind`; "" when the file list could not be read.
        reads: the boundaries `limit` reads.
    """

    kind: str
    reads: Reads

    def limit(self, live_kinds: list[str]) -> tuple[int, str]:
        """The limit beside the kinds of the landings already live, and what set it."""
        cap, why = base()
        for lowered, reason in self._drops(live_kinds):
            if lowered < cap:
                cap, why = lowered, reason
        return cap, why

    def _drops(self, live_kinds: list[str]) -> list[tuple[int, str]]:
        reads, now = self.reads, self.reads.now()
        drops = []
        quota = _read(reads.core_quota)
        if quota:
            remaining, reset = quota
            resets_in = reset - int(now.timestamp())
            if remaining < QUOTA_FLOOR and resets_in > QUOTA_RESET_FAR_S:
                drops.append(
                    (
                        1,
                        f"the core quota has {remaining} calls left and resets in {resets_in // 60}m",
                    )
                )
        runs = _read(reads.master_runs)
        slow = _read(
            lambda: pace.count_unfinished(runs, now.timestamp(), SLOW_RUN_AGE_S)
        )
        if slow is not None and slow >= SLOW_RUNS:
            drops.append(
                (
                    SLOW_RUNS_LIMIT,
                    f"{slow} master CI runs unfinished for over {SLOW_RUN_AGE_S // 60}m",
                )
            )
        if self.kind:
            hold = _read(lambda: pin_gate.hold_problem(reads.hold()))
            if hold:
                drops.append((0, f"{hold}, and this PR changes a k8s role"))
            if _read(lambda: reads.in_window(now)):
                drops.append(
                    (
                        1,
                        "inside the weekly reboot's no-promotion window, for a k8s change",
                    )
                )
        if self.kind == SERIAL:
            drops.append(
                (1, "this PR changes a forward-only pin, flux or cluster-vars")
            )
        elif SERIAL in live_kinds:
            drops.append((1, "a landing of a pin, flux or cluster-vars change is live"))
        return drops


def admission(pr: str, reads: Reads) -> Admission:
    """`pr`'s admission, reading its file list once."""
    paths = _read(lambda: reads.pr_paths(pr))
    return Admission(kind(paths) if paths else "", reads)
