"""Step 5, when `deploy.sh --at <merge sha>` exits 4: deploy the newest green descendant instead.

Exit 4 under `--at` means a later commit reaches the same tags. The path from before this
module (wait for the tick, deploy from the primary checkout) cannot converge while PRs merge
every few minutes: #5459's landing ended `tip-outran-retries` three times (#5486). The newest
green commit that contains the merge commit is a CI-gated tree that reverts nothing, so it
is deployed with `--at` and the primary checkout is not touched.

When that descendant goes stale too and no newer one is green yet, the retarget waits for one
rather than falling back: a shared k8s role such as arr-notification reaches every k8s tag, so
every landing stays stale until a green commit contains it, and the fallback's 60+120+240 s
is shorter than master CI takes while CI is saturated (#5517).
"""

from collections.abc import Callable

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.exit_codes import DEPLOY_STALE
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import say


def retarget(ln: Landing, rc: int, deploy: Callable[..., int]) -> int:
    """Redeploy `--at` the newest green descendant of the merge commit while `--at` goes stale.

    Returns the last deploy's exit code, or `rc` untouched when no green descendant exists to
    try; the caller then falls back to the tick and the primary checkout. Each attempt needs
    a descendant NEWER than the tree that just went stale, and `stale_retries` bounds them.

    While the newest green descendant is still the tree that just went stale, the retarget
    polls for a newer one every `lock_backoff` seconds. The polls are not attempts: one
    `ci_timeout` budget covers every poll of the call, counted from the seconds slept, and the
    caller falls back once it runs out. The slept seconds are booked under `t_ci` and
    `t_tick`, as the fallback's own CI wait is, so the board does not read them as deploy time.

    `deployed_at` becomes the retargeted commit, because the health gate renders the tree
    that was deployed (`health_verdict.gate_from_the_deployed_tree`). `deployed_hosts` is
    cleared with it: one landing renders one tree, and a host deployed at the merge commit
    would otherwise be skipped and left on it. `deploy` is `deploy.deploy_with_lock_retry`,
    passed in because `deploy` imports this module.
    """
    o = ln.opts
    budget = o.ci_timeout
    for _ in range(o.stale_retries):
        ln.fetch_branch()
        green = ln.tools.newest_green_descendant(ln.merge_sha)
        while green and green == ln.deployed_at and budget > 0:
            wait = min(o.lock_backoff, budget)
            say(
                f"{green[:12]} is still the newest green commit that contains "
                f"{ln.merge_sha[:12]}; checking again in {wait}s ({budget}s of {o.ci_timeout}s left)"
            )
            ln.tools.sleep(wait)
            budget -= wait
            ln.ledger.t_ci = (ln.ledger.t_ci or 0.0) + wait
            ln.ledger.t_tick = (ln.ledger.t_tick or 0.0) + wait
            ln.fetch_branch()
            green = ln.tools.newest_green_descendant(ln.merge_sha)
        if not green or green == ln.deployed_at:
            break
        say(
            f"a later commit reaches {ln.tags_csv}; deploying {green[:12]}, the newest green "
            f"commit that contains {ln.merge_sha[:12]}"
        )
        ln.deployed_at = green
        ln.deployed_hosts.clear()
        rc = deploy(ln, at=green)
        if rc != DEPLOY_STALE:
            break
    return rc
