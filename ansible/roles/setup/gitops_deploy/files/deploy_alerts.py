# ansible/roles/setup/gitops_deploy/files/deploy_alerts.py
"""The deployer's notify subsystem: every message body, the queue's file, and when to send.

**What to say** is one named pure function per alert — so the text is testable without driving
a tick, and a 1900-character budget can be asserted against the assembled post rather than
guessed at. **When to say it** is the second half of the file: `alert_once` owns the per-SHA
dedupe marker, `deliver` owns the retry queue, and `alert_deferred` fires the tasks/meta/k8s
channels. Both halves used to be split across `gitops_deploy.py`.

`deploy_health.py` holds the queue's pure half (`apply_send_result`, `cap_pending`,
`apply_drain_result`); this module is its I/O counterpart plus the composers.

The webhook itself is `deploy_toolbox.post`, reached through `tools.discord_post`. It sits
there rather than here because it is a process boundary AND because this module imports
`deploy_toolbox` for the `DeployTools` it takes — the old edge in the other direction would
have been a cycle.

Reach these functions qualified (`deploy_alerts.alert_once(...)`), never by from-import —
see `deploy_io.py`'s docstring for why.
"""

import json

import deploy_io
from deploy_changes import ChangeSet
from deploy_config import Config, log
from deploy_failtext import failing_task, head, tail
from deploy_health import (
    PENDING_ALERTS_MAX,
    apply_drain_result,
    apply_send_result,
    cap_pending,
)
from deploy_remediation import deferred_service_alerts, k8s_remediation
from deploy_state import DeployerState
from deploy_toolbox import DeployTools
from host_lib import atomic_write

# Per-alert budget for an embedded error string. host_lib.discord_post cuts a post at
# `message[:1900]`, keeping the HEAD — so an unbounded error string does not truncate itself, it
# evicts the remediation prose that follows it. Sized for the longest of the three failure posts.
ALERT_EXCERPT_CHARS = 700


# ── the webhook, and the queue's file ─────────────────────────────────────────────────────────


def read_pending(path: str) -> dict[str, str]:
    """The queued-but-undelivered alerts, or {} when the file is absent or unreadable as JSON."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    # Split (not `except (A, B)`): ruff (3.14 target, from requires-python) reformats a
    # parenthesized tuple into the 3.14-only `except A, B:` form. Two clauses give ruff nothing
    # to rewrite. Still load-bearing: unlike its siblings this unit has NOT yet moved to the
    # pinned 3.14 (docs/archive/host-python-314-plan.md, task 6), so it runs on the host's 3.12 today and
    # the rewritten form would SyntaxError. Keep the split until that task lands.
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def write_pending(path: str, pending: dict[str, str]) -> None:
    """Persist the queue. Atomic (temp + rename, see host_lib): a torn write mustn't strand it."""
    atomic_write(path, json.dumps(pending))


# ── error text, bounded for a Discord post ────────────────────────────────────────────────────


def alert_excerpt(exc: BaseException, limit: int = ALERT_EXCERPT_CHARS) -> str:
    """Render ``exc`` for a Discord post: the argv line, then the failing task or the tail.

    The first line of a run() RuntimeError is the argv and the exit code, which a plain tail
    would drop. The rest is process output, and run() put the failing task at the start of it
    when there was one, so a tail here would evict it again behind stderr's deprecation
    warnings — the same output a positional tail is the wrong slice of.
    """
    first, newline, body = str(exc).partition("\n")
    first = first[:limit]
    if not newline:
        return first
    budget = max(limit - len(first), 0)
    found = failing_task(body)
    if found is None:
        return f"{first}\n{tail(body, budget)}"
    return f"{first}\n{head(found[0], budget)}"


# ── the message bodies ────────────────────────────────────────────────────────────────────────
# One function per alert. Pure, so `tests/test_gitops_deploy_failure_output.py` can assert the
# assembled post stays inside host_lib.discord_post's 1900-character cut with its remediation
# line intact — the reason `broad_failure_alert` was a function before the rest of them were.


def crash_alert(exc: BaseException) -> str:
    """The generic page for an unexpected exception, sent before the traceback propagates."""
    return f"🚨 gitops-deploy crashed: {exc}"


def bad_config_alert(hostname: str, config_path: str, exc: BaseException) -> str:
    """The page for a config file the deployer cannot be run with.

    A malformed numeric value raised during IMPORT until `Config` moved the parse behind
    `load_config`, so the failure reached an operator as a traceback with no key name in it and
    no Discord post at all — the unit's OnFailure curl was the whole signal.
    """
    return (
        f"🚨 gitops-deploy: **bad config** on {hostname}. `{config_path}` — {exc}.\n"
        f"No tick ran. Re-render it with `uv run ansible-playbook "
        f"ansible/initial_setup.yml --tags gitops_deploy`."
    )


def broad_failure_alert(
    hostname: str,
    playbook: str,
    tags: list[str],
    origin: str,
    exc: BaseException,
    hold_file: str,
    hold_plane_file: str,
) -> str:
    """The post for a failed broad apply.

    A function rather than an inline f-string so the 1900-char budget it is sized against is
    testable — see tests/test_gitops_deploy_failure_output.py.
    """
    tag_note = f" --tags `{','.join(tags)}`" if tags else ""
    return (
        f"🚨 gitops-deploy: **broad apply failed** on {hostname}.\n"
        f"`{playbook}`{tag_note} errored on `{origin[:8]}`:\n"
        f"```\n{alert_excerpt(exc)}\n```\n"
        f"The tree is fast-forwarded and the plane is **unapplied**. This arm is "
        f"forward-only — **nothing was rolled back**, so live state is whatever the "
        f"failed run left.\n"
        f"**Action:** fix forward and re-run that playbook by hand. A later tick clears the "
        f"hold only by applying this same plane — after a hand run, "
        f"`rm {hold_file} {hold_plane_file}`."
    )


def broad_deferred_alert(origin: str, remediation: str) -> str:
    """The post for a broad change this deployer will not apply itself."""
    return (
        f"⚠️ gitops-deploy: broad change needing a hand in `{origin[:8]}` — "
        f"deferring to a manual deploy. On the host, run "
        f"{remediation} "
        f"to clear it."
    )


def secrets_deferred_alert(origin: str) -> str:
    """The post for a `secrets.yml` change that ff-merged with no consumer redeployed."""
    return (
        f"⚠️ gitops-deploy: `secrets.yml` changed in `{origin[:8]}` with no "
        f"service template — fast-forwarded but **nothing was redeployed**. The "
        f"rotated secret won't reach its container(s) until you redeploy them "
        f"(`ansible-playbook ansible/deploy.yml --tags <svc>`)."
    )


def tasks_deferred_alert(origin: str, services: set[str]) -> str:
    """The post for a structural-dir change to services this tick did not redeploy."""
    return (
        f"⚠️ gitops-deploy: a structural dir (`tasks/`/`defaults/`/`vars/`/`handlers/`) changed "
        f"for `{', '.join(sorted(services))}` in `{origin[:8]}` with no redeploy of those "
        f"service(s) — fast-forwarded but **not applied** (those dirs aren't auto-deployed). "
        f"Redeploy by hand: `ansible-playbook ansible/deploy.yml --tags <svc>`."
    )


def meta_deferred_alert(origin: str, services: set[str]) -> str:
    """The post for a `meta/deps.yml` change to services this tick did not redeploy."""
    return (
        f"⚠️ gitops-deploy: `meta/deps.yml` changed for "
        f"`{', '.join(sorted(services))}` in `{origin[:8]}` with no redeploy of those "
        f"service(s) — fast-forwarded but **not applied** (meta/ isn't auto-deployed; it "
        f"changes deploy ordering + dep closure). Redeploy the affected service(s) by hand: "
        f"`ansible-playbook ansible/deploy.yml --tags <svc>`."
    )


def k8s_deferred_alert(
    origin: str, k8s: set[str], declared_k8s: set[str], consumers: set[str] | None
) -> str:
    """The post for a k8s role change, which this deployer never auto-deploys.

    The remediation half is `deploy_remediation.k8s_remediation`, which decides whether the
    change can be named as a `--tags` redeploy at all.
    """
    return (
        f"⚠️ gitops-deploy: k8s role(s) `{', '.join(sorted(k8s))}` changed in "
        f"`{origin[:8]}` — fast-forwarded but **not applied** (this deployer only "
        f"auto-deploys Docker-platform services; k8s roles are defer-and-alert). "
    ) + k8s_remediation(k8s, declared_k8s, consumers)


def stale_composes_alert(hostname: str, stale: list[str]) -> str:
    """The post for a rendered compose with no containers_list entry."""
    return (
        f"⚠️ gitops-deploy: stale rendered compose(s) on {hostname} with no "
        f"containers_list entry: `{', '.join(stale)}` — a retired/migrated service "
        f"left its render behind, and its phantom containers will fail the health "
        f"gate on that service's next deploy (false rollback + hold). Clean up: "
        f"`docker rm -f <its containers>` then `rm -rf containers/<svc>`."
    )


def dirty_tree_alert(hostname: str) -> str:
    """The twice-daily reminder that an operator's edit is parking this host."""
    return (
        f"⚠️ gitops-deploy: working tree dirty on {hostname} — skipping. "
        "Resolve manually."
    )


def ci_failed_alert(hostname: str, local: str, origin: str) -> str:
    """The post for a master tip whose CI is red."""
    return (
        f"⛔ gitops-deploy: CI is RED on `{origin[:8]}` — NOT deploying on {hostname}. "
        f"The host stays on `{local[:8]}` until master is green; fix forward or revert. "
        "(GitOps Status pages separately once the host has been behind for 6h.)"
    )


def stale_denylist_alert(origin: str, detail: str, fix: str) -> str:
    """The post for a config.env denylist that disagrees with the declarations at origin."""
    return (
        f"⚠️ gitops-deploy: `/etc/gitops-deploy/config.env` denylist is stale against "
        f"`{origin[:8]}` — {detail}. k8s auto-deploy is DISARMED until this is fixed: "
        f"{fix}."
    )


def staging_verdict_alert(origin: str, summary: str, blocking: bool) -> str:
    """The post for any staging verdict that is not a PASS."""
    tail_line = (
        "This gate BLOCKS — prod was not deployed unless the verdict was no_verdict."
        if blocking
        else "Prod deployed regardless — this gate does not block yet."
    )
    return f"🧪 gitops-deploy: {summary} for `{origin[:8]}`. {tail_line}"


def staging_override_alert(hostname: str, origin: str, override_file: str) -> str:
    """The post announcing that the operator's one-tick override was spent."""
    return (
        f"🔓 gitops-deploy: **staging override used** on {hostname}. "
        f"Staging REJECTED `{origin[:8]}` and it was deployed to prod anyway, "
        f"because `{override_file}` was armed.\n"
        f"The override is now spent — re-arm it with `touch` if the next tick "
        f"needs it too."
    )


def staging_rejected_alert(
    hostname: str, local: str, origin: str, services: set[str], override_file: str
) -> str:
    """The post for a blocking staging rejection: nothing was applied, nothing to undo."""
    return (
        f"🧪 gitops-deploy: **staging REJECTED** `{origin[:8]}` on {hostname} — "
        f"prod was NOT deployed and the tree stays on `{local[:8]}`.\n"
        f"`{', '.join(sorted(services))}` failed on daniel-stage. Nothing was "
        f"applied here, so there is nothing to roll back and no volume was "
        f"reverted.\n"
        f"**Action:** fix forward on master, or — if staging itself is the "
        f"problem rather than the change — `touch {override_file}` to let "
        f"the next tick through once.\n"
        f"The hold only skips THIS commit: `skip_hold` matches while "
        f"`origin_head == hold_sha`, so the next push past it is gated afresh."
    )


def k8s_failure_alert(
    hostname: str,
    local: str,
    origin: str,
    services: set[str],
    exc: BaseException,
    revert_note: str,
) -> str:
    """The post for a k8s deploy that failed its rollout gate and was rolled back."""
    return (
        f"🚨 gitops-deploy: **k8s deploy failed** on {hostname}.\n"
        f"`{', '.join(sorted(services))}` from `{origin[:8]}` failed its rollout "
        f"gate:\n```\n{alert_excerpt(exc)}\n```\n"
        f"Rolled back locally to `{local[:8]}`.\n"
        f"{revert_note}"
        f"**The bad pin is still live on master.** The hold only skips THIS commit — "
        f"`skip_hold` matches while `origin_head == hold_sha`, so the next push past it "
        f"redeploys the same pin.\n"
        f"**Action:** revert the offending commit on the remote, or pin the bad version "
        f"out via Renovate `allowedVersions`."
    )


def deploy_failure_alert(
    hostname: str, local: str, origin: str, services: set[str], exc: BaseException
) -> str:
    """The post for an `ansible-playbook` that errored deploying Docker services."""
    return (
        f"🚨 gitops-deploy: **deploy failed** on {hostname}.\n"
        f"`ansible-playbook` errored deploying `{', '.join(sorted(services))}` from "
        f"`{origin[:8]}`:\n```\n{alert_excerpt(exc)}\n```\n"
        f"Rolled back to `{local[:8]}`; the bad commit is held until origin advances past it.\n"
        f"**Action:** fix or revert the offending commit."
    )


def rollback_alert(hostname: str, local: str, origin: str, failed: list[str]) -> str:
    """The post for a Docker health gate that failed and rolled the host back."""
    return (
        f"🚨 gitops-deploy: **rollback** on {hostname}.\n"
        f"Service(s) `{', '.join(failed)}` from commit `{origin[:8]}` failed the health "
        f"gate and were rolled back to `{local[:8]}`.\n"
        f"**Action:** revert the offending Renovate PR — the bad commit is held until you do."
    )


def discord(tools: DeployTools, config: Config, content: str) -> bool:
    """Post to the alert webhook. False on any failure, so the alert is retried next tick."""
    return tools.discord_post(config.discord_webhook, content)


def deliver(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    key: str,
    content: str,
) -> bool:
    """Post an alert now, queuing it (keyed by "<channel>:<sha>") for retry on a delivery failure.

    A transient webhook blip can't permanently drop it — the ff-merged secrets/tasks/meta/
    combined paths never re-reach their alert code on the next (noop) tick, so `discord()`'s
    own 'retry next tick' doesn't hold for them. drain_pending() resends any queued entry every
    tick. Returns discord()'s result.

    The queue write happens BEFORE the send, so a process death during discord() leaves the
    alert queued rather than lost — see the DECIDED note below.
    """
    pending = read_pending(state.path("pending_alerts"))
    # DECIDED: queue BEFORE the send, not after. discord() blocks for up to 10s in urlopen, and
    # alert_once has already advanced its per-SHA marker by the time we get here — so a process
    # death inside that window (a reboot, a `systemctl stop`, the UPS shutdown chain) used to leave
    # a durable "already alerted" marker with nothing delivered and nothing queued, and the
    # ff-merged channels never re-reach their alert code on a later tick. Queue-first trades
    # lost-on-crash for duplicate-on-crash: a death after the 2xx but before the removal write below
    # makes drain_pending() repost once. At-least-once is the right side for an alert.
    queued = apply_send_result(pending, key, content, False)
    if queued != pending:
        # Deliberately uncapped: capping here could evict a real backlogged alert to make room for
        # one that is about to be delivered anyway. The queue may sit at PENDING_ALERTS_MAX + 1 for
        # the length of one discord() call; the post-send write below is what enforces the cap.
        write_pending(state.path("pending_alerts"), queued)
    delivered = discord(tools, config, content)
    # `queued`, NOT `pending`, is the baseline from here on. Comparing the removal against the
    # pre-queue dict would make it a permanent no-op, so the entry would never leave and every
    # alert would repost on every tick.
    updated = apply_send_result(queued, key, content, delivered)
    updated, dropped = cap_pending(updated)
    for stale in dropped:
        # Logged, never silent: this is an alert being discarded undelivered, which is the exact
        # outcome the queue exists to prevent. A backlog this deep means the webhook itself has
        # been broken for over a day, and DISCORD_CONSECUTIVE has been paging about that.
        log(
            f"pending-alert queue over {PENDING_ALERTS_MAX}; dropping oldest undelivered {stale}"
        )
    if updated != queued:
        write_pending(state.path("pending_alerts"), updated)
    return delivered


def drain_pending(tools: DeployTools, state: DeployerState, config: Config) -> None:
    """Resend every queued-but-undelivered alert.

    Runs first thing each tick — BEFORE the noop/hold/dirty short-circuits — so an alert whose
    original tick ff-merged (local==origin -> the next tick noops) still gets redelivered. Clears
    each entry on a confirmed 2xx.
    """
    pending = read_pending(state.path("pending_alerts"))
    if not pending:
        return
    delivered = {k for k, c in pending.items() if discord(tools, config, c)}
    updated = apply_drain_result(pending, delivered)
    if updated != pending:
        write_pending(state.path("pending_alerts"), updated)


def alert_once(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    marker: str,
    channel: str,
    origin: str,
    content: str,
) -> None:
    """Deliver a per-SHA-deduped alert on `channel`.

    Args:
        marker: the `DeployerState` marker holding the last SHA alerted on this channel.
        channel: the queue key's prefix.
        origin: the SHA being alerted about.
        content: the message body, from `deploy_alerts`.

    No-op if this origin SHA was already alerted (marker == origin). Otherwise mark DETECTION here
    (advance the marker once per SHA) and hand delivery + retry to deliver()/the pending queue — the
    marker advances on DETECTION, NOT delivery, so a transient webhook blip is redelivered by
    drain_pending() rather than silently dropped, and an ff-merged path that noops next tick doesn't
    re-page.
    """
    if state.read(marker) == origin:
        return
    state.write(marker, origin)
    deliver(tools, state, config, f"{channel}:{origin}", content)


def alert_secrets_deferred(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    origin: str,
    cs: ChangeSet,
) -> None:
    """Alert (once per SHA) that `secrets.yml` was ff-merged with no consumer redeployed.

    Split out of the no-services branch on 2026-08-24 so the k8s auto-deploy path can fire it too.
    That path ff-merges, deploys the promoted service and returns without ever reading cs.secrets,
    so a rotation push and a Renovate image bump landing in the same 30-minute window arrive as ONE
    ChangeSet and the rotated secret goes silently stale — and because the merge already happened,
    no later tick re-evaluates it.

    Why it is safe to fire on the k8s path but NOT on the Docker deploy path: a k8s service is
    promoted to auto-deploy only when its sole changed path is defaults/main.yml — image-bump-only
    by construction (see split_k8s_auto_deploy) — so a promoted service can never itself be the
    secret's consumer. The Docker path is the opposite case: the /add-secret flow ships secrets.yml
    WITH its consuming template, so the consumer IS in cs.services and alerting there would
    false-fire on the happy path. That asymmetry is why this is a separate helper rather than a
    line inside alert_deferred(), which runs on both.
    """
    if not cs.secrets:
        return
    alert_once(
        tools,
        state,
        config,
        "secrets_alerted",
        "secrets",
        origin,
        secrets_deferred_alert(origin),
    )


def alert_deferred(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    origin: str,
    deployed: set[str],
    cs: ChangeSet,
    declared_k8s: set[str] | None = None,
) -> None:
    """Fire the tasks/, meta/deps.yml, and k8s-role defer-and-alert for changes not redeployed.

    Runs on BOTH the no-services branch (deployed=set()) and after a SUCCESSFUL deploy
    (deployed=cs.services): a combined push (svcA template + svcB meta/deps.yml) deploys svcA but
    leaves svcB's deploy-graph change ff-merged and unapplied. The pending remainder is the pure
    `deferred_service_alerts`; this is its I/O shell (per-SHA dedupe marker + deliver). Each channel
    alerts at most once per origin SHA; its marker advances on DETECTION (deliver() and the pending
    queue own delivery + retry), so a transient webhook blip is redelivered, not silently dropped.

    `declared_k8s` is this host's `platform: k8s` containers_list entries, used to decide whether
    the k8s alert can name a `--tags` redeploy at all (see k8s_remediation). It defaults to None
    for the caller that has not read the inventory; None is treated as the EMPTY set, which makes
    every changed role read as untaggable and prescribes a full deploy. That is the fail-safe
    direction: a full deploy is slower than necessary but always applies the change, whereas a
    `--tags` line for a role with no entry exits 0 having applied nothing.
    """
    declared_k8s = declared_k8s or set()
    pending_tasks, pending_meta = deferred_service_alerts(cs, deployed)
    if pending_tasks:
        alert_once(
            tools,
            state,
            config,
            "tasks_alerted",
            "tasks",
            origin,
            tasks_deferred_alert(origin, pending_tasks),
        )
    if pending_meta:
        alert_once(
            tools,
            state,
            config,
            "meta_alerted",
            "meta",
            origin,
            meta_deferred_alert(origin, pending_meta),
        )
    if cs.k8s:
        # No `- deployed` subtraction (unlike tasks/meta): this deployer never auto-deploys a
        # k8s-platform role at all, so there's no scoped redeploy for a k8s change to have ridden.
        #
        # DECIDED: this alert is a one-shot detection, not the durable signal. It fires once per
        # origin SHA (alert_once) and the ff-merge below clears `behind_since` -- the deployer's
        # own "still behind" marker -- so every other monitored marker reads clean while the
        # cluster keeps running the old manifests (issue #947). The durable signal is a daniel-box
        # cron reading `probe.py releases --stale-only` against the release records
        # `roles/k8s/manifests/tasks/release_stamp.yml` writes on every real apply -- see this
        # role's CLAUDE.md, "k8s-platform roles are auto-deployed ONLY for an image-pin bump...".
        alert_once(
            tools,
            state,
            config,
            "k8s_alerted",
            "k8s",
            origin,
            k8s_deferred_alert(origin, cs.k8s, declared_k8s, cs.k8s_consumers),
        )


def check_stale_composes(
    tools: DeployTools, state: DeployerState, config: Config
) -> None:
    """Page (once per distinct set) when a rendered compose has no matching containers_list entry.

    containers/<svc>/docker-compose.yml exists on disk but <svc> has no containers_list entry —
    the stale-compose trap (see deploy_inventory.stale_rendered_services for the incident
    history). Detection only, never cleanup: the remedy removes containers and directories, which
    stays an operator action.
    """
    stale = deploy_io.stale_composes(config.repo, config.hostname)
    if stale is None:
        return  # unreadable inventory/tree — not this watchdog's failure to page about
    marker = ",".join(stale)
    if state.read("stale_composes") == (marker or None):
        return
    state.write("stale_composes", marker or None)
    if stale:
        deliver(
            tools,
            state,
            config,
            f"stale-composes:{marker}",
            stale_composes_alert(config.hostname, stale),
        )
