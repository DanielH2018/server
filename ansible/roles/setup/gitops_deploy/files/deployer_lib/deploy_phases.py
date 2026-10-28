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

import re
from collections.abc import Mapping

from deployer_lib import deploy_inventory
from deployer_lib import deploy_io
from deployer_lib import deploy_cross_role
from deployer_lib import deploy_defer
from deployer_lib import deploy_setup_roles
from deployer_lib.deploy_changes import (
    comment_only_broad_changes,
    comment_only_k8s_defaults,
    services_from_changed_paths,
)
from deployer_lib.deploy_config import ERR, WARNING, Config, log
from deployer_lib.deploy_git import ci_walk_candidates, is_diverged, next_action
from deployer_lib.deploy_inventory import declares_no_gitops
from deployer_lib.deploy_state import DeployerState
from deployer_lib.deploy_tick_types import (
    NotTheDeployerHost,
    RetryableFetchError,
    TickPlan,
    TickTarget,
)
from deployer_lib.deploy_toolbox import DeployTools


def refuse_unless_deployer(config: Config) -> None:
    """Raise NotTheDeployerHost when this host's own host_vars say `has_gitops: false`.

    The first thing `main()` does, ahead of the alert drain and every state write: the
    inventory is the same source the role's `when: has_gitops` gate reads, so a payload the
    role has stopped maintaining refuses on its own rather than ticking from stale logic
    (#1733). `declares_no_gitops` carries the fail-open rules; this is only the read.
    """
    if declares_no_gitops(deploy_io.host_vars_text(config.repo, config.hostname)):
        raise NotTheDeployerHost(
            f"{config.hostname} declares has_gitops: false in its host_vars — not a GitOps "
            "deployer; refusing to tick. Reap this payload with "
            "`initial_setup.yml --tags gitops_deploy`."
        )


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
    # The window is real, not theoretical: `deploy_lib/run.py:staleness_gate` runs deploy_lib/staleness.py (which
    # fetches) BEFORE it takes the git-tree lock, and --dry-run returns before the
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
    # next_action's own short-circuits above it, so a noop/dirty/held tick costs no API request.
    # A tick that would deploy costs one request for the tip. The 10-min timer makes that up to
    # 6 an hour. An authenticated tick whose tip is not green adds the ancestor walk below.
    ci = "pass"
    tip, tip_ci = origin, "pass"
    if not dirty and origin_ahead and origin != local and origin != hold:
        ci = tip_ci = tools.fetch_ci_verdict(origin)
        # A HELD tip skips the walk with everything else, and stays `skip_hold`. The condition
        # above is unchanged: ruling (d) asks only that a held SHA is never CHOSEN, which
        # `ci_walk_candidates` does by dropping it from the candidates.
        if ci in ("pending", "fail"):
            green = _newest_green_ancestor(tools, config, local, origin, hold, ci)
            if green is not None:
                origin, ci = green, "pass"
    return TickTarget(
        local=local,
        origin=origin,
        hold=hold,
        dirty=dirty,
        status=status.stdout,
        action=next_action(local, origin, hold, dirty, origin_ahead, ci),
        tip=tip,
        tip_ci=tip_ci,
    )


def _newest_green_ancestor(
    tools: DeployTools,
    config: Config,
    local: str,
    tip: str,
    hold: str | None,
    tip_ci: str,
) -> str | None:
    """The newest commit below `tip` whose own CI is green, or None if the walk finds none.

    Master takes about 124 merges a day against a ~103s CI sweep, so the tip is pending on
    most ticks that would otherwise deploy; gating on it deferred a green commit behind every
    later merge's sweep. The chosen SHA becomes `target.origin` and everything downstream
    reads it — the ff-merge, the changed-path diff, the declarations read, the narrowing —
    so the walk chooses ONE SHA exactly as the pin above does. The REAL tip stays on
    `TickTarget.tip`, and `entrypoint()` re-resolves it for `behind_since`, so a tail that
    never goes green still pages through the 6h behind-origin watchdog.

    Returns None on a git failure as well as on an all-red walk: the tip's own verdict then
    decides the tick exactly as it did before this existed, which is the fail-closed direction.

    It also returns None on an UNAUTHENTICATED host, before spending anything. The walk costs
    up to `CI_ANCESTOR_WALK_MAX` GitHub reads on one tick, which is nothing against an
    authenticated 5000/hour and a sixth of the anonymous hourly budget the whole host shares
    with every landing's `await_ci.py` poll. Exhausting that budget reads as "CI not finished"
    everywhere, so the walk would buy one tick's latency by deferring the next several.
    """
    walk_max = config.ci_ancestor_walk_max
    if walk_max <= 0:
        return None
    if not tools.github_authenticated():
        log(
            f"ci: origin {tip[:8]} is {tip_ci}; not walking for a green ancestor — this host has "
            "no GitHub token, and the anonymous 60/hour limit is shared with every landing"
        )
        return None
    try:
        rev_list = tools.run(
            ["git", "rev-list", "--first-parent", f"{local}..{tip}"], cwd=config.repo
        ).split()
    except Exception as exc:
        log(f"ci: could not list commits below {tip[:8]}: {type(exc).__name__}: {exc}")
        return None
    candidates = ci_walk_candidates(rev_list, hold, walk_max)
    for behind, sha in candidates:
        if tools.fetch_ci_verdict(sha) != "pass":
            continue
        log(
            f"ci: origin {tip[:8]} is {tip_ci}; fast-forwarding to the newest green ancestor "
            f"{sha[:8]} ({behind} behind the tip)"
        )
        return sha
    # Said on every deferring tick, because the cost is what an operator reading a repeated
    # deferral needs: a walk that read nine ancestors and found no green one is a different
    # state from a tip with nothing below it, and both defer silently otherwise.
    log(
        f"ci: origin {tip[:8]} is {tip_ci}; no green ancestor in the {len(candidates)} "
        f"commit(s) below it that this tick could read"
    )
    return None


def plan_tick(
    tools: DeployTools, state: DeployerState, config: Config, target: TickTarget
) -> TickPlan:
    """Classify the incoming range into the ChangeSet this tick will act on.

    Runs BEFORE the ff-merge, so every read here is at the pinned `origin` rather than the
    working tree.
    """
    paths = tools.run(
        ["git", "diff", "--name-only", f"{target.local}..{target.origin}"],
        cwd=config.repo,
    ).splitlines()

    # A comment-only edit to a bring-up playbook is not a change the deployer must park on;
    # it parked three sessions' landings on 2026-09-02 (PR #746) until an operator ff-merged
    # by hand. The paths dropped here would have set broad_manual by prefix alone.
    def show(ref: str, p: str) -> str:
        return tools.run(["git", "show", f"{ref}:{p}"], cwd=config.repo)

    quiet = comment_only_broad_changes(paths, target.local, target.origin, show)
    if quiet:
        log(
            f"classify: comment-only change in {', '.join(sorted(quiet))} — "
            "not parking; the tick treats it as no change"
        )
    # The same read for a k8s role's defaults (#4460). The tick applies no k8s role since
    # D6.12, so this only keeps the journal from naming an edit that moved nothing.
    quiet_k8s = comment_only_k8s_defaults(paths, target.local, target.origin, show)
    if quiet_k8s:
        log(
            f"classify: comment-only change in {', '.join(sorted(quiet_k8s))} — "
            "not deferring; the tick treats it as no change"
        )
        quiet |= quiet_k8s
    if quiet:
        paths = [p for p in paths if p not in quiet]
    if deploy_cross_role.CROSS_ROLE_FILE in paths:
        _adopt_incoming_cross_role_tables(tools, config, target.origin)
    adopt_setup_routing(tools, config, target.origin)
    cs = deploy_defer.drop_deleted_setup_roles(
        tools, config, target.origin, services_from_changed_paths(paths), paths
    )
    deploy_defer.route_secret_readers(tools, config, target, cs)
    _, owners = deploy_io.host_vars_owners(config, target.origin, run=tools.run)
    deploy_inventory.route_not_ansible_owned(cs, owners)
    for tag in sorted(cs.flux):
        reached = [p for p in paths if p.startswith(f"ansible/roles/k8s/{tag}/")]
        log(
            f"flux: {tag}: owned by {owners[tag]} at {target.origin[:8]} — fast-forwarding "
            f"{', '.join(sorted(reached))}; promote applies it, so this tick does not (D2.2)"
        )
    return TickPlan(cs=cs, paths=paths, owners=owners)


def _adopt_incoming_cross_role_tables(
    tools: DeployTools, config: Config, origin: str
) -> None:
    """Classify a range that edits the cross-role tables with origin's copy of them (#3512).

    A read or parse failure keeps the installed copy, which is the behaviour before #3512.
    """
    path = deploy_cross_role.CROSS_ROLE_FILE
    try:
        source = tools.run(["git", "show", f"{origin}:{path}"], cwd=config.repo)
        deploy_cross_role.use_tables(deploy_cross_role.tables_in(source))
    except Exception as exc:
        log(
            f"classify: range edits {path}, and its copy at {origin[:8]} is unusable "
            f"({exc}) — classifying with the installed tables"
        )
        return
    log(f"classify: range edits {path} — classifying with its copy at {origin[:8]}")


def adopt_setup_routing(tools: DeployTools, config: Config, origin: str) -> None:
    """Route setup roles by the playbooks at `origin`, or route none (#3734).

    Read at origin, not the working tree, because this runs before the ff-merge: a range that
    adds a role and its playbook entry together routes by the new entry. A failure routes
    nothing rather than guessing, where a guessed `--tags` that matched nothing would exit 0
    and record an apply of nothing (PR #702). A failure is transient, so a range carrying a
    setup role parks and the next tick retries. A role the routing cannot place is a
    deterministic answer, so the tick records it in `manual_plane` and does not park (#4326).
    """
    try:
        routes, unplaced = tools.setup_routing(config.repo, origin, config.hostname)
    except Exception as exc:
        deploy_setup_roles.use_routing({}, failed=True)
        log(
            f"setup-routing: routing at {origin[:8]} failed ({type(exc).__name__}: {exc}) — "
            "a range carrying a setup role parks until a tick can route it",
            level=WARNING,
        )
        return
    deploy_setup_roles.use_routing(routes)
    for role, why in sorted(unplaced.items()):
        log(
            f"setup-routing: setup role {role} cannot be routed at {origin[:8]} ({why}) — "
            "a range carrying it records it in manual_plane for a hand"
        )


# Roles under roles/k8s/ that deploy no service of their own — they are included by other roles
# and carry no defaults/main.yml, so they declare nothing. Mirrors SHARED_ROLES in
# ansible/filter_plugins/k8s_autodeploy.py; ansible/tests/deploy/test_denylist_parsers_agree.py asserts
# the two stay in step. It moved here from `deploy_k8s.py` with `declared_denylist` (D6.12).
SHARED_K8S_ROLES = frozenset({"manifests"})

# A top-level `k8s_autodeploy:` assignment, with an optional trailing comment. Anchored at column
# zero deliberately: an indented key of the same name belongs to some other mapping and does not
# declare the role's stance. `[ \t]+` (not `*`) requires a space after the colon, so
# `k8s_autodeploy:true` — which is not a dict key YAML would resolve this way either — doesn't
# match and falls through to "no declaration found", i.e. denied.
_DECLARATION_RE = re.compile(
    r"^k8s_autodeploy:[ \t]+(?P<value>\S+)[ \t]*(?:#.*)?$", re.MULTILINE
)
# The spellings PyYAML resolves to boolean true. Everything else — including an unparseable or
# absent value — counts as denied, so this parser can never widen the denylist's complement.
_TRUE_VALUES = frozenset(
    {"true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"}
)


def declared_denylist(sources: Mapping[str, str | None]) -> frozenset[str]:
    """Roles denied auto-deploy, read from each role's own `k8s_autodeploy` declaration.

    `sources` maps a role name to the text of its defaults/main.yml, or None when the role has
    no such file. Shared roles are skipped; every other role is denied unless EVERY line that
    looks like a top-level `k8s_autodeploy:` assignment reads as true, and at least one such
    line was found.

    That's unanimity, not "last match wins" — a role is permitted only if every candidate
    assignment agrees. YAML itself is last-key-wins for a genuine duplicate key, so unanimity is
    not what a real YAML parser would do; it's deliberately more conservative. This is a regex,
    not a YAML parser: on adversarial or malformed input (a decoy `true` before the real
    `false`, a duplicate key) unanimity can only ever add denials, never turn a role the
    last-wins reading would deny into one this reader calls permitted.

    Its one caller is `reconcile_denylist`, which detects that the deployer's rendered config
    has gone stale against the checkout. The authoritative derivation is the Ansible filter,
    which parses real YAML; ansible/tests/deploy/test_denylist_parsers_agree.py pins the two
    together. D6.2a deletes both readers.
    """
    denied = set()
    for role, text in sources.items():
        if role in SHARED_K8S_ROLES:
            continue
        if not text:
            denied.add(role)
            continue
        values = [m.group("value") for m in _DECLARATION_RE.finditer(text)]
        if not values or any(v not in _TRUE_VALUES for v in values):
            denied.add(role)
    return frozenset(denied)


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


def reconcile_denylist(
    tools: DeployTools, state: DeployerState, config: Config, head: str
) -> bool:
    """Re-render config.env when its denylist disagrees with the checkout it was rendered from.

    `K8S_AUTODEPLOY_DENYLIST` is derived from every role under roles/k8s/ at RENDER time, and
    the only thing that re-renders it is `initial_setup.yml --tags gitops_deploy`. A change
    under roles/k8s/ matches no prefix that runs that playbook, so adding a role declaring
    `k8s_autodeploy: false` left the baked list stale (issues #1265, #1294).

    This keeps config.env matching the declarations at the checkout's own HEAD. Stating it
    against HEAD rather than origin is what makes it fixable — the render reads the working
    tree, so re-rendering can only ever produce HEAD's list. The tick deploys no k8s role since
    D6.12, so nothing in it reads the list any more; `land_renovate_pr.py:denied_roles` still
    reads the rendered line until D6.2a repoints it, and D6.2a deletes this function.

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
        declared = declared_denylist(
            deploy_io.k8s_declarations_at(config.repo, head, run=tools.run)
        )
    except Exception as exc:
        # No marker write: an unreadable ref is transient, so the next tick tries again.
        log(
            f"denylist: could not read k8s declarations at {head[:8]}: {type(exc).__name__}: {exc}",
            level=WARNING,
        )
        return False
    # DECIDED: an EMPTY `declared` is damage, never evidence — guard it before the comparison,
    # unconditionally, rather than only when the config denylist is empty too. A `git ls-tree`
    # that lists no role under roles/k8s/ reads as `frozenset()`, which against a config whose
    # denylist line was lost — also `frozenset()` — compared EQUAL and marked the damaged config
    # reconciled for that checkout, so the repair never ran (issue #1331, reachable since #1321
    # moved this gate to the file-level flag). The authoritative derivation takes the same stance
    # for the same reason: `filter_plugins/k8s_autodeploy.py:156-160` raises rather than render an
    # empty denylist, because an empty result means the derivation is broken. No marker write, as
    # in the unreadable-ref branch above: an empty listing is one `git ls-tree` and zero `git
    # show` reads, so retrying every tick is cheap — do not "fix" the log line by claiming the
    # SHA, which is exactly the bug. The log wording differs from the branch above so an operator
    # can tell an empty listing from a git failure.
    if not declared:
        log(
            f"denylist: empty k8s declaration read at {head[:8]}: no role under ansible/roles/k8s/ "
            "declared a stance, which is a broken read rather than an empty denylist "
            "— leaving config.env alone and retrying next tick",
            level=WARNING,
        )
        return False
    if declared == config.k8s_autodeploy_denylist:
        state.write("denylist_rendered", head)
        return False
    # Written BEFORE the run, not after: a render that times out or is SIGTERMed mid-play must
    # not be retried every tick against the same checkout.
    state.write("denylist_rendered", head)
    log(
        f"denylist: config.env denylist is stale against the checkout at {head[:8]} "
        f"(denied at HEAD but not in config: {sorted(declared - config.k8s_autodeploy_denylist) or 'none'}; "
        f"in config but not at HEAD: {sorted(config.k8s_autodeploy_denylist - declared) or 'none'}) "
        "— re-rendering it"
    )
    try:
        # Through `tools.run`, so the argv above is what a test asserts on.
        tools.run(
            RENDER_CONFIG_ARGV, cwd=config.repo, timeout=config.broad_deploy_timeout_s
        )
    except Exception as exc:
        # DECIDED: a failed self-render does NOT write hold_sha, unlike `handle_broad`'s failed
        # apply. The two failures contain differently. A half-applied broad plane leaves live
        # state nothing recorded, so parking is the containment. This render only rewrites the
        # deployer's own config.env; a failure leaves the OLD file, which is the state the tick
        # already tolerates. Parking every unrelated apply behind a config render would be a
        # strictly larger outage than the one this heals.
        log(f"denylist: re-render failed: {type(exc).__name__}: {exc}", level=ERR)
        return True
    log("denylist: config.env re-rendered — the next tick reads the fresh denylist")
    return True
