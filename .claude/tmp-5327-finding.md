D5.1d (#5327) gave `promote.py land-wait` a fourth outcome, `suspended`. When one of the PR's
tags is `suspended` in the merge commit's `digests.json`, the wait ends at once, and `land.sh`
prints `nothing-to-deploy` with exit 0. A tag the merge commit changed while it was suspended at
both ends is not covered by this outcome. It waits for `decide`'s `suspended-changed` hold and
ends `deploy-failed`.

Contract C6 in `docs/target-architecture/contracts.md` (*`land.sh`*, *Cluster*) still lists only
`deploy-failed` and `gave-up` as the ways the wait ends. #5327 left that file alone because #5320
was editing it in parallel.

The fix is one sentence in C6's *Cluster* bullet naming the `suspended` outcome and its
`nothing-to-deploy` landing. `scripts/deploy_tools/promote_lib/wait.py`'s docstring and
`docs/landing.md` already state it.
