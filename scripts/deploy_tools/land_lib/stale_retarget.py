"""Step 5, when `deploy.sh --at <merge sha>` exits 4: deploy the newest green descendant instead.

Exit 4 under `--at` means a later commit reaches the same tags. The path from before this
module (wait for the tick, deploy from the primary checkout) cannot converge while PRs merge
every few minutes: #5459's landing ended `tip-outran-retries` three times (#5486). The newest
green commit that contains the merge commit is a CI-gated tree that reverts nothing, so it
is deployed with `--at` and the primary checkout is not touched.
"""

from collections.abc import Callable

from lib.exit_codes import DEPLOY_STALE
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import say


def retarget(ln: Landing, rc: int, deploy: Callable[..., int]) -> int:
    """Redeploy `--at` the newest green descendant of the merge commit while `--at` goes stale.

    Returns the last deploy's exit code, or `rc` untouched when no green descendant exists to
    try; the caller then falls back to the tick and the primary checkout. Each attempt needs
    a descendant NEWER than the tree that just went stale, and `stale_retries` bounds them.

    `deployed_at` becomes the retargeted commit, because the health gate renders the tree
    that was deployed (`health_verdict.gate_from_the_deployed_tree`). `deployed_hosts` is
    cleared with it: one landing renders one tree, and a host deployed at the merge commit
    would otherwise be skipped and left on it. `deploy` is `deploy.deploy_with_lock_retry`,
    passed in because `deploy` imports this module.
    """
    for _ in range(ln.opts.stale_retries):
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
