# ansible/roles/setup/gitops_deploy/files/deploy_alerts.py
"""The deployer's notify subsystem: when to send, the queue's file, and the send itself.

**When to say it** is this module: `alert_once` owns the per-SHA dedupe marker, `deliver` owns
the retry queue, `drain_pending` empties it, and `alert_deferred` fires the tasks/meta/k8s
channels. **What to say** is `deploy_alert_text.py`, one pure function per alert — the seam
this module's docstring named before #2600 split it, so that neither half sits at the
600-line module cap.

`deploy_health.py` holds the queue's pure half (`apply_send_result`, `cap_pending`,
`apply_drain_result`); this module is its I/O counterpart.

The webhook itself is `deploy_toolbox.post`, reached through `tools.discord_post`. It sits
there rather than here because it is a process boundary AND because this module imports
`deploy_toolbox` for the `DeployTools` it takes — the old edge in the other direction would
have been a cycle.

Reach these functions qualified (`deploy_alerts.alert_once(...)`), never by from-import —
see `deploy_io.py`'s docstring for why.
"""

import json

import deploy_alert_text
import deploy_io
from deploy_changes import ChangeSet
from deploy_config import Config, log
from deploy_health import (
    PENDING_ALERTS_MAX,
    apply_drain_result,
    apply_send_result,
    cap_pending,
)
from deploy_remediation import deferred_service_alerts
from deploy_state import DeployerState
from deploy_toolbox import DeployTools
from host_lib import atomic_write


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
        deploy_alert_text.secrets_deferred_alert(origin),
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
            deploy_alert_text.tasks_deferred_alert(origin, pending_tasks),
        )
    if pending_meta:
        alert_once(
            tools,
            state,
            config,
            "meta_alerted",
            "meta",
            origin,
            deploy_alert_text.meta_deferred_alert(origin, pending_meta),
        )
    if cs.k8s:
        # No `- deployed` subtraction (unlike tasks/meta): a bump this tick deployed sits in
        # `cs.k8s_deploy`. The one exception is a broad tick's narrowed deploy plane, and
        # `deploy_broad_k8s` subtracts what that applied from `cs` before calling this (#2453).
        #
        # DECIDED: this alert is a one-shot detection, not the durable signal. It fires once per
        # origin SHA (alert_once) and the ff-merge below clears `behind_since`, so every other
        # monitored marker reads clean while the cluster keeps running the old manifests (#947).
        # TWO durable signals carry it: the `k8s_unapplied` marker, which the caller one frame up
        # (`deploy_defer.alert_and_record_deferred`) writes for the hand-edited and denylisted
        # classes (#2570), and the daniel-box cron reading `probe.py releases --stale-only`
        # against the records `roles/k8s/manifests/tasks/release_stamp.yml` writes on a real apply.
        alert_once(
            tools,
            state,
            config,
            "k8s_alerted",
            "k8s",
            origin,
            deploy_alert_text.k8s_deferred_alert(
                origin, cs.k8s, declared_k8s, cs.k8s_consumers
            ),
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
            deploy_alert_text.stale_composes_alert(config.hostname, stale),
        )
