# ansible/roles/setup/gitops_deploy/files/deploy_health.py
"""The Docker health gate and the Discord delivery queue.

`containers_to_gate` reads the rendered compose for every `container_name:` the gate must
poll, `health_decision` / `health_settles` / `gate_services` turn the poll samples into a
verdict, and `apply_send_result` / `cap_pending` / `apply_drain_result` keep the pending-alert
file bounded across ticks.
"""

from __future__ import annotations

import re

# A `container_name:` line in a rendered docker-compose.yml.
_CONTAINER_NAME = re.compile(r'^\s*container_name:\s*["\']?([^\s"\']+)["\']?\s*$')


def container_names(compose_text: str) -> list[str]:
    """Every `container_name:` declared in a rendered docker-compose.yml, in order.

    The deployer health-gates these, not the role/service name: a single role
    often runs several containers and the Renovate-bumped image's container is
    usually NOT the role-named one (e.g. `cadvisor` lives in the `prometheus`
    role, `scrutiny-influxdb` in `scrutiny`).
    """
    out: list[str] = []
    for line in compose_text.splitlines():
        m = _CONTAINER_NAME.match(line)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def containers_to_gate(compose_text: str | None, service: str) -> list[str]:
    """Containers to health-gate for `service` after a deploy.

    `compose_text` is the service's rendered docker-compose.yml on THIS host, or
    None when that file doesn't exist — which means the service isn't deployed on
    this host (e.g. dozzle is daniel-pi-only, and the deployer doesn't run on the
    Pi at all — has_gitops: false there).
    A changed template for such a service renders nothing here, so we must gate
    nothing: returning [] makes the caller skip it instead of polling a phantom
    container until HEALTH_TIMEOUT_S and triggering a false rollback.

    A present compose that declares no `container_name` falls back to [service].
    """
    if compose_text is None:
        return []
    return container_names(compose_text) or [service]


def health_decision(
    health_status: str, running: bool, running_streak: int, settle_checks: int = 3
) -> tuple[str, int]:
    """Pure transition for ONE health poll of a just-deployed container.

    This is the pass-or-keep-waiting decision the deployer's poll loop (`health_ok`
    in gitops_deploy.py) makes on each sample, lifted out of the I/O so it can be
    unit-tested without Docker/sleep/wall-clock. Inputs:
      - health_status: docker `.State.Health.Status` — 'healthy' / 'starting' /
        'unhealthy', or '' for an image with NO HEALTHCHECK (also '' if the
        container is already gone).
      - running: docker `.State.Running` (only consulted in the no-healthcheck
        case; pass False otherwise).
      - running_streak: count of consecutive prior no-healthcheck 'running' samples.
    Returns (verdict, new_running_streak); verdict is 'healthy' (gate passes — stop
    polling) or 'wait' (keep polling until the deadline).

    The settle streak is the boot-then-crash guard: a no-healthcheck image must stay
    'running' across `settle_checks` consecutive polls before it counts as healthy,
    so a container that boots then crash-loops can't slip the gate the way a single
    'running' sample would.
    """
    if health_status == "healthy":
        return "healthy", running_streak
    if health_status == "":  # no healthcheck -> require sustained running
        new_streak = running_streak + 1 if running else 0
        if new_streak >= settle_checks:
            return "healthy", new_streak
        return "wait", new_streak
    # 'starting' / 'unhealthy' -> not yet; reset the streak and keep waiting.
    return "wait", 0


def health_settles(samples: list[tuple[str, bool]], settle_checks: int = 3) -> bool:
    """Fold `health_decision` over a sequence of (health_status, running) polls.

    True if the container would reach 'healthy' before the samples run out (the poll
    loop returns True and the deploy stands); False if it never settles within them
    (the loop hits HEALTH_TIMEOUT_S and the deployer rolls back to the prior HEAD).
    A pure mirror of `health_ok`'s loop with the I/O (docker inspect + sleep + the
    deadline) removed, so the streak/crash-loop logic is exercised in tests.
    """
    streak = 0
    for health_status, running in samples:
        verdict, streak = health_decision(health_status, running, streak, settle_checks)
        if verdict == "healthy":
            return True
    return False


def apply_send_result(
    pending: dict[str, str], key: str, content: str, delivered: bool
) -> dict[str, str]:
    """The pending-alert queue after attempting to send `content` under `key`.

    On a confirmed delivery the key is cleared; on a failure the content is (re)queued under it, so a
    transient webhook blip can't permanently drop a post-merge alert (the ff-merged secrets/tasks/meta
    paths never re-reach their alert code). Pure: the caller (`deliver` in gitops_deploy.py) does the
    discord() I/O and persists the result only when it differs from the input. Returns a NEW dict;
    `pending` is not mutated.
    """
    updated = dict(pending)
    if delivered:
        updated.pop(key, None)
    else:
        updated[key] = content
    return updated


# The pending-alert queue had no cap, no expiry and no timestamps. Nothing reads the file back
# except drain_pending(), so an entry only leaves on a confirmed 2xx — a webhook that stays broken
# (a revoked URL, a permanently wrong channel) grows it without bound on a 30-minute tick, and
# every tick then re-POSTs the whole backlog. 64 is the cap: alerts are Discord messages, so the
# file stays well under a megabyte, and a backlog deeper than 64 means the webhook has been broken
# for over a day — at which point the OLDEST alerts are the least worth keeping.
#
# Eviction is oldest-first with no timestamp needed: dicts preserve insertion order, and json.load
# preserves it on the way back in, so the queue's own order IS its age order.
PENDING_ALERTS_MAX = 64


def cap_pending(
    pending: dict[str, str], max_entries: int = PENDING_ALERTS_MAX
) -> tuple[dict[str, str], list[str]]:
    """The queue trimmed to `max_entries`, plus the keys dropped — oldest first.

    Returns the dropped keys rather than dropping them silently: an alert discarded without a
    trace is the failure this queue exists to prevent, one level up. Pure; the caller logs.
    """
    if max_entries <= 0 or len(pending) <= max_entries:
        return pending, []
    dropped = list(pending)[: len(pending) - max_entries]
    return {k: c for k, c in pending.items() if k not in set(dropped)}, dropped


def apply_drain_result(pending: dict[str, str], delivered: set[str]) -> dict[str, str]:
    """The queue after a drain pass in which the `delivered` keys were confirmed sent — every other
    entry is kept for the next tick. Pure; the caller (`drain_pending`) does the per-entry discord()
    I/O and persists only on a change.
    """
    return {k: c for k, c in pending.items() if k not in delivered}


def gate_services(services, health_fn, gate_deadline, now_fn) -> list[str]:
    """Health-gate `services` (sorted, deterministic) and return those that FAILED.

    Bounds the TOTAL wall-clock spent gating: once `now_fn()` reaches `gate_deadline`, it
    stops polling and marks the current service AND every still-ungated one as failed, so the
    gate + rollback (git reset + one redeploy) finishes inside the unit's TimeoutStartSec.
    Without this cap a multi-service batch with several containers each polling to
    HEALTH_TIMEOUT_S can overrun the timeout; systemd then SIGTERMs the deployer before the
    rollback + hold run and the bad commit is left live (next tick sees local==origin -> noop).

    `health_fn(service, gate_deadline)` returns True when healthy; it also receives the deadline
    so one slow container's own poll loop can't block past it. A service not deployed on this
    host is vacuously healthy (health_fn returns True — see containers_to_gate). `now_fn` is the
    injected clock (the deployer passes `time.time`; tests pass a fake) — keeps this module I/O-free.
    """
    failed: list[str] = []
    ordered = sorted(services)
    for i, service in enumerate(ordered):
        if now_fn() >= gate_deadline:
            failed.extend(ordered[i:])
            break
        if not health_fn(service, gate_deadline):
            failed.append(service)
    return failed
