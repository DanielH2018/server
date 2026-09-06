#!/usr/bin/env python3
"""The phases that run before a tick knows which branch it is on.

`assess` reads both HEADs and classifies the tick into a `TickTarget`; `plan_tick` turns the
incoming range into the `TickPlan` one `deploy_handlers` phase then acts on. Neither deploys:
both read, decide and record, and `gitops_deploy.main()` owns the order.

`reconcile_denylist` is the one exception, and runs between them. It applies exactly one
playbook — `initial_setup.yml --tags gitops_deploy`, which re-renders this deployer's OWN
config.env — and it deploys no service. It sits here because it acts on the checkout the two
phases around it have just read, not on a change set.

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


# The one command that re-derives K8S_AUTODEPLOY_DENYLIST — the filter plugin reads every role
# under roles/k8s/ at render time — and the same command the stale-denylist alert has always told
# an operator to run.
#
# `gitops_deploy_kick_after_change=false` is load-bearing, not tidiness. Rendering config.env
# notifies the role's "Run gitops-deploy once" handler, which runs `systemctl start
# gitops-deploy.service` and BLOCKS until that job finishes. This render runs INSIDE that same
# Type=oneshot unit, so systemd coalesces the request into the activation already in flight and
# the handler would wait on the tick that is waiting on it — a self-deadlock broken only by the
# timeout. The kick exists so a first install activates without a manual `systemctl start`; a
# tick that is already running needs no kick. ENFORCED by
# ansible/tests/deploy/test_denylist_render_suppresses_the_kick.py.
RENDER_CONFIG_ARGV = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/initial_setup.yml",
    "--tags",
    "gitops_deploy",
    "-e",
    "gitops_deploy_kick_after_change=false",
]


def reconcile_denylist(state: DeployerState, config: Config, head: str) -> bool:
    """Re-render config.env when its denylist disagrees with the checkout it was rendered from.

    `K8S_AUTODEPLOY_DENYLIST` is derived from every role under roles/k8s/ at RENDER time, and
    the only thing that re-renders it is `initial_setup.yml --tags gitops_deploy`. A change
    under roles/k8s/ matches no prefix that runs that playbook, so adding a role declaring
    `k8s_autodeploy: false` left the baked list stale and `_promote_k8s_auto_deploys` disarmed
    auto-deploy FLEET-WIDE until an operator re-rendered by hand — measured on game-stats-lib,
    2026-09-05, disarmed 12:30 to 18:29 UTC (issues #1265, #1294).

    This is the local half of that invariant: config.env must match the declarations at the
    checkout's own HEAD. Stating it against HEAD rather than origin is what makes it fixable —
    the render reads the working tree, so re-rendering can only ever produce HEAD's list. The
    origin-side comparison in `_promote_k8s_auto_deploys` stays exactly as it was and remains
    the fail-safe for the window where origin is ahead: this heals on disk, and the tick that
    follows the ff-merge is the one that reads the fresh config.

    Returns:
        True when a re-render ran (whether or not it succeeded), False when nothing was needed.
    """
    # DECIDED: the gate is the FILE-level flag (`k8s_autodeploy_enabled_in_file`), never the
    # post-disarm `k8s_autodeploy_enabled`. `gitops_deploy.py` flips the latter to False when the
    # rendered denylist is empty — fail-closed, so a truncated config.env cannot widen what
    # auto-deploys. Gating here on the flipped value made that one state unhealable: a config.env
    # that LOST its denylist line disarmed the very reconcile whose re-render is the repair, and
    # the host stayed that way until an operator ran `initial_setup.yml --tags gitops_deploy`
    # (issue #1317). Reading the file-level flag keeps the three states apart — the file says
    # off, so skip; the file says on with no denylist, so re-render; the file says on with a
    # denylist, so compare. A host that legitimately has auto-deploy off still renders nothing,
    # on any tick, because its file flag is false.
    if not config.k8s_autodeploy_enabled_in_file:
        return False
    if state.read("denylist_rendered") == head:
        # Both halves of the once-per-SHA guard: the per-role `git show` reads below (64 roles as of 2026-09-06) are skipped on
        # every idle tick, and a mismatch a re-render CANNOT fix — a config rendered from an
        # unpushed tree — re-renders once for that checkout instead of every ten minutes.
        return False
    try:
        declared = declared_denylist(deploy_io.k8s_declarations_at(config.repo, head))
    except Exception as exc:
        # No marker write: an unreadable ref is transient, so the next tick tries again.
        log(
            f"could not read k8s declarations at {head[:8]}: {type(exc).__name__}: {exc}"
        )
        return False
    if declared == config.k8s_autodeploy_denylist:
        state.write("denylist_rendered", head)
        return False
    # Written BEFORE the run, not after: a render that times out or is SIGTERMed mid-play must
    # not be retried every tick against the same checkout.
    state.write("denylist_rendered", head)
    log(
        f"config.env denylist is stale against the checkout at {head[:8]} "
        f"(denied at HEAD but not in config: {sorted(declared - config.k8s_autodeploy_denylist) or 'none'}; "
        f"in config but not at HEAD: {sorted(config.k8s_autodeploy_denylist - declared) or 'none'}) "
        "— re-rendering it"
    )
    try:
        # Built here rather than in deploy_io because that module is at its length ratchet;
        # it still reaches `deploy_io.run` qualified, which is the one boundary the suite
        # patches, so the argv above is what a test asserts on.
        deploy_io.run(
            RENDER_CONFIG_ARGV, cwd=config.repo, timeout=config.broad_deploy_timeout_s
        )
    except Exception as exc:
        # DECIDED: a failed self-render does NOT write hold_sha, unlike `handle_broad`'s failed
        # apply. The two failures contain differently. A half-applied broad plane leaves live
        # state nothing recorded, so parking is the containment. This render only rewrites the
        # deployer's own config.env; a failure leaves the OLD file, which is the state the tick
        # already tolerates — auto-deploy stays disarmed by the origin comparison, which is the
        # fail-safe direction. Parking every unrelated service deploy behind a config render
        # would be a strictly larger outage than the one this heals.
        log(f"denylist re-render failed: {type(exc).__name__}: {exc}")
        return True
    log("config.env re-rendered — the next tick reads the fresh denylist")
    return True


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
