#!/usr/bin/env python3
"""The two phases that run before a tick knows which branch it is on.

`assess` reads both HEADs and classifies the tick into a `TickTarget`; `plan_tick` turns the
incoming range into the `TickPlan` one `deploy_handlers` phase then acts on. Nothing here
deploys: both read, decide and record, and `gitops_deploy.main()` owns the order.

Each takes the tick's `tools`, `state` and `config` — the `deploy_config.Config`
`gitops_deploy.tick_config()` snapshots once per tick, never the entry module itself.

Reach `deploy_io` and `deploy_alerts` qualified, never by from-import.
"""

import deploy_alerts
import deploy_io
from deploy_changes import (
    ChangeSet,
    comment_only_broad_changes,
    services_from_changed_paths,
    shared_module_consumers,
)
from deploy_config import Config, log
from deploy_git import is_diverged, next_action
from deploy_inventory import declared_k8s_services, reroute_k8s_services
from deploy_k8s import (
    declared_denylist,
    declares_snapshot_claims,
    is_image_only_diff,
    split_k8s_auto_deploy,
)
from deploy_state import DeployerState
from deploy_tick_types import RetryableFetchError, TickPlan, TickTarget
from deploy_toolbox import DeployTools


def assess(tools: DeployTools, state: DeployerState, config: Config) -> TickTarget:
    """Read git, decide what kind of tick this is, and manage the divergence marker.

    Returns:
        A `TickTarget` carrying both HEADs, the hold, the dirty state and `next_action`'s word.

    Raises:
        RetryableFetchError: `git status` or `git fetch` failed. entrypoint() skips the tick
            cleanly on it, without writing last_run.
    """
    # A dirty working tree (operator may be mid-edit) is a healthy skip, not an outage: we never
    # deploy from it, but the tick completes and writes last_run so a long edit session doesn't
    # falsely trip the GitOps-Alive monitor. (git fetch is safe on a dirty tree — it only updates
    # remote-tracking refs.) Skipping is safe precisely because it does NOT write last_run: a
    # checkout that is genuinely broken keeps failing, ages the marker past GITOPS_MAX_AGE_S and
    # still pages via GitOps-Alive ~60min later, instead of double-paging 48x/day forever.
    status = tools.git_status(config.repo)
    if status.returncode != 0:
        raise RetryableFetchError(
            status.stderr.strip() or f"git status exited {status.returncode}"
        )
    dirty = bool(status.stdout.strip())

    fetch = tools.git_fetch(config.repo, config.branch)
    if fetch.returncode != 0:
        raise RetryableFetchError(
            fetch.stderr.strip() or f"git fetch exited {fetch.returncode}"
        )
    local = tools.run(["git", "rev-parse", "HEAD"], cwd=config.repo)
    # Pinned ONCE, and every decision below plus every merge uses this value rather than
    # re-resolving `origin/<branch>`. The CI verdict, the changed-path diff, the denylist read and
    # the broad marker all evaluate against this exact commit; a merge that re-resolved the ref
    # could land a DIFFERENT one, and `--ff-only` would happily accept it because it is still a
    # descendant. That commit's CI was never checked (REQUIRE_CI defaults true), its paths were
    # never classified — and because the tree then equals origin, next_action() returns "noop"
    # from that point on, so it is never deployed and never defer-and-alerted either, with the
    # hold marker and the behind-origin watchdog both reading green.
    #
    # The window is real, not theoretical: `scripts/deploy.sh` runs deploy_staleness.py (which
    # fetches) BEFORE it takes /var/lock/server-git-tree.lock, and --dry-run returns before the
    # lock entirely — so a dry run in another session moves this repo's remote-tracking ref
    # mid-tick. The ref lives in the shared .git dir every worktree points at.
    origin = tools.run(["git", "rev-parse", f"origin/{config.branch}"], cwd=config.repo)
    hold = state.hold_sha

    # origin is "ahead" only if local is an ancestor of it — i.e. it carries commits we don't
    # have. If origin is behind (the operator committed locally but hasn't pushed) or the two
    # diverged, there is nothing to fast-forward and next_action() makes this a no-op instead of
    # mis-firing on the reverse diff.
    origin_ahead = tools.is_ancestor(config.repo, local, origin)
    # Divergence watchdog: if local and origin differ but neither is an ancestor of the other, the
    # deployer can't fast-forward and every tick noops while origin's new commits never deploy —
    # invisible otherwise (last_run keeps ticking, no hold). Record it so GitOps Status pages; clear
    # it once resolved. A committed-but-unpushed local commit (local_ahead — secret-rotate's domain)
    # is a plain noop, NOT flagged here. Managed every tick regardless of `action`.
    local_ahead = tools.is_ancestor(config.repo, origin, local)
    state.write(
        "diverged",
        origin if is_diverged(origin, local, origin_ahead, local_ahead) else None,
    )
    # Only spend the GitHub call on a tick that would otherwise deploy. These conditions mirror
    # next_action's own short-circuits above it, so a noop/dirty/held tick costs no API request —
    # which keeps the gate's share of the GitHub rate limit at one request per 30 min.
    ci = "pass"
    if not dirty and origin_ahead and origin != local and origin != hold:
        ci = tools.fetch_ci_verdict(origin)
    return TickTarget(
        local=local,
        origin=origin,
        hold=hold,
        dirty=dirty,
        status=status.stdout,
        action=next_action(local, origin, hold, dirty, origin_ahead, ci),
    )


def plan_tick(
    tools: DeployTools, state: DeployerState, config: Config, target: TickTarget
) -> TickPlan:
    """Classify the incoming range into the ChangeSet this tick will act on.

    Runs BEFORE the ff-merge, so every read here is at the pinned `origin` rather than the
    working tree — see `deploy_io.k8s_declarations_at`.
    """
    paths = tools.run(
        ["git", "diff", "--name-only", f"{target.local}..{target.origin}"],
        cwd=config.repo,
    ).splitlines()
    # A comment-only edit to a bring-up playbook is not a change the deployer must park on;
    # it parked three sessions' landings on 2026-09-02 (PR #746) until an operator ff-merged
    # by hand. The paths dropped here would have set broad_manual by prefix alone.
    quiet = comment_only_broad_changes(
        paths,
        target.local,
        target.origin,
        lambda ref, p: tools.run(["git", "show", f"{ref}:{p}"], cwd=config.repo),
    )
    if quiet:
        log(
            f"comment-only change in {', '.join(sorted(quiet))} — "
            "not parking; the tick treats it as no change"
        )
        paths = [p for p in paths if p not in quiet]
    cs = services_from_changed_paths(paths)
    cs.k8s_consumers = shared_module_consumers(paths, config.repo)
    # A path under ansible/roles/containers/<svc>/ maps to <svc> by NAME ALONE — it doesn't know
    # this host might run that same-named service under k8s (wg-easy: a Docker role, but
    # platform: k8s on daniel-box). Route those into the k8s defer-and-alert set instead of
    # deploying a tag that resolves to deploy.yml's K8S play (an idempotent no-op whose health
    # gate silently no-ops too, since containers_for() renders nothing for a k8s entry).
    hostvars = deploy_io.host_vars_text(config.repo, config.hostname)
    k8s_services = declared_k8s_services(hostvars) if hostvars is not None else set()
    cs = reroute_k8s_services(cs, k8s_services)
    cs = _promote_k8s_auto_deploys(tools, state, config, cs, paths, target)
    return TickPlan(cs=cs, paths=paths, k8s_services=k8s_services)


def _promote_k8s_auto_deploys(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    cs: ChangeSet,
    paths: list[str],
    target: TickTarget,
) -> ChangeSet:
    """Move image-bump-only k8s changes from defer-and-alert into the auto-deploy channel.

    Disarms itself first when this host's baked denylist disagrees with the declarations at
    origin: that config is rendered only by `initial_setup.yml --tags gitops_deploy`, while a
    declaration flip lands under roles/k8s/ and alerts naming `deploy.yml` — a playbook that
    never re-renders it. Without the check the host would keep acting on the old list, leaving a
    role that was just denied still auto-deployable. Disarm loudly rather than acting on a stale
    boundary.
    """
    autodeploy_enabled = config.k8s_autodeploy_enabled
    k8s_defaults_at_origin: dict[str, str | None] = {}
    if autodeploy_enabled:
        try:
            # `target.origin` (the SHA pinned in assess(), not f"origin/{config.branch}") — the diff and
            # the alert already evaluate against that exact commit; re-resolving the ref here
            # would open a TOCTOU where a concurrent fetch lands between the two reads.
            k8s_defaults_at_origin = deploy_io.k8s_declarations_at(
                config.repo, target.origin
            )
            declared = declared_denylist(k8s_defaults_at_origin)
            read_error = None
        except Exception as exc:
            k8s_defaults_at_origin = {}
            declared = None
            read_error = f"{type(exc).__name__}: {exc}"
            log(f"could not read k8s declarations at origin: {read_error}")
        if declared is None or declared != config.k8s_autodeploy_denylist:
            autodeploy_enabled = False
            if declared is not None:
                added = sorted(declared - config.k8s_autodeploy_denylist)
                removed = sorted(config.k8s_autodeploy_denylist - declared)
                detail = (
                    f"denied at origin but not in config: {added or 'none'}; "
                    f"in config but not at origin: {removed or 'none'}"
                )
                # Both directions are usually "config is behind origin" and want the same fix:
                # a re-render. `added` means a role was newly denied at origin; `removed` means a
                # role was PROMOTED there — the denylist shrank — which this host has not picked
                # up yet. `removed` has one other cause, an operator who rendered locally before
                # pushing, so it names that as a secondary check. Naming `git push` FIRST on
                # `removed` was wrong: it is the less common cause and the fix does nothing for
                # the other one, which is what a promotion looks like.
                fix = (
                    "run `uv run ansible-playbook ansible/initial_setup.yml --tags "
                    "gitops_deploy` on the host (`deploy.yml` does not re-render config.env)"
                )
                if removed and not added:
                    fix += (
                        ". If that changes nothing, the config was rendered from an unpushed "
                        "tree instead — `git push` it and re-render"
                    )
            else:
                detail = f"the declarations at origin could not be read ({read_error})"
                fix = "check the ref/path on the host — this clears on its own once it reads again"
            log(f"k8s auto-deploy disarmed — stale denylist ({detail})")
            deploy_alerts.alert_once(
                tools,
                state,
                config,
                "stale_denylist_alerted",
                "stale_denylist",
                target.origin,
                deploy_alerts.stale_denylist_alert(target.origin, detail, fix),
            )
    # Everything not promoted stays in cs.k8s and defer-and-alerts exactly as before, so this is
    # inert until a service passes BOTH the diff-shape test and the denylist.
    return split_k8s_auto_deploy(
        cs,
        paths,
        denylist=config.k8s_autodeploy_denylist,
        pilot=config.k8s_autodeploy_pilot,
        enabled=autodeploy_enabled,
        image_only=lambda svc: is_image_only_diff(
            deploy_io.k8s_image_diff(config.repo, target.local, target.origin, svc)
        ),
        max_per_tick=config.k8s_autodeploy_max_per_tick,
        # Read at the PINNED origin, like the denylist above and for the same reason — the
        # promotion decision runs before the ff-merge, so the working tree still holds the
        # pre-merge declarations. `.get(svc)` (not `[svc]`): a role absent from the listing is
        # already denied by the stale-denylist comparison, and an absent entry must not raise
        # here.
        declares_claims=lambda svc: declares_snapshot_claims(
            k8s_defaults_at_origin.get(svc)
        ),
        max_claim_services_per_tick=config.k8s_autodeploy_max_claim_services_per_tick,
    )
