#!/usr/bin/env python3
"""What one phase of a tick hands the next.

Two frozen dataclasses and the one exception `assess` raises, no behaviour. `TickTarget` is
what `deploy_phases.assess` found when it looked at git; `TickPlan` is what the incoming range
means once classified.

The settings every phase reads are `deploy_config.Config`, not a type of this module's own:
`gitops_deploy.tick_config()` snapshots that module's globals back onto the frozen `Config`
once per tick, which is what lets the test suite keep repointing them on the entry module.

`gitops_deploy` re-exports `RetryableFetchError` under its own name, because `assess` moved
here while `entrypoint` — the one thing that catches it — did not, and a leaf may not import
the entry module. The class object is this one; the re-export is a second name for it, so
`except gitops_deploy.RetryableFetchError` still catches what `assess` raises.

This is a leaf: `deploy_changes` for the `ChangeSet` type, the standard library, nothing else.
"""

from dataclasses import dataclass

from deploy_changes import ChangeSet


class RetryableFetchError(Exception):
    """A transient git failure: `git fetch origin`, or `git status` unable to read the tree.

    entrypoint() turns this into a CLEAN skip of the tick — exit 0, NO in-script Discord
    crash-page, NO OnFailure — that also does NOT refresh last_run. So a one-off blip is
    silently retried next tick, while a PERSISTENT fetch failure still surfaces via
    GitOps-Alive going stale over several missed ticks. Distinct from a real crash (unexpected
    exception), which still pages. Before this, a `run()`-raised fetch error propagated to
    entrypoint() and double-paged (the crash Discord + the OnFailure unit) every 30-min tick for
    the whole duration of a GitHub-side incident.
    """


@dataclass(frozen=True)
class TickTarget:
    """What `assess()` found when it looked at git, and what `next_action` made of it.

    Attributes:
        local: the commit this checkout is on.
        origin: `origin/<branch>`, resolved ONCE — see `assess()` for why re-resolving it
            anywhere below would open a window for an unchecked commit to deploy.
        hold: the SHA this host refuses to redeploy, or None.
        dirty: whether `git status --porcelain` reported anything, untracked files included.
        status: that command's raw stdout, so the dirty branch can name the paths.
        action: `next_action`'s word — noop, dirty, skip_hold, ci_pending, ci_failed, deploy.
    """

    local: str
    origin: str
    hold: str | None
    dirty: bool
    status: str
    action: str


@dataclass(frozen=True)
class TickPlan:
    """What the incoming range means for this host, once classified.

    Attributes:
        cs: the ChangeSet, after k8s rerouting and the auto-deploy promotion split.
        paths: the changed paths, minus any comment-only broad change dropped as quiet.
        k8s_services: this host's `platform: k8s` containers_list entries, which decide
            whether a deferred k8s alert can name a `--tags` redeploy at all.
    """

    cs: ChangeSet
    paths: list[str]
    k8s_services: set[str]
