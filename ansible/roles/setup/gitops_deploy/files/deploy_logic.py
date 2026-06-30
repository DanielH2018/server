# ansible/roles/setup/gitops_deploy/files/deploy_logic.py
"""Pure decision logic for the GitOps deployer (no I/O — unit-tested).

`services_from_changed_paths` maps a git-diff file list to the set of active
container services to redeploy, or flags a "broad" change (shared template /
inventory) that the deployer must defer to a manual full deploy.

`next_action` decides what a poll tick should do given the local/origin HEADs
and any recorded known-bad (hold) SHA.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A bind-mounted file under an active container role's templates/ or files/ dir — the
# docker-compose.yml.j2 OR any config template / files/ asset (e.g. prometheus.yml.j2,
# authelia configuration.yml.j2, monitor-bridge/files/check.py). A change here only reaches the
# container on its next deploy, so it maps to a scoped, health-gated redeploy — closing the GitOps
# loop instead of a silent ff-merge. tasks/ and the role CLAUDE.md are deliberately NOT matched
# (structural / docs — deploy those manually). The negative lookahead excludes archive/<svc>/...
_ACTIVE_CONFIG = re.compile(
    r"^ansible/roles/containers/(?!archive/)([^/]+)/(?:templates|files)/"
)
# A `container_name:` line in a rendered docker-compose.yml.
_CONTAINER_NAME = re.compile(r'^\s*container_name:\s*["\']?([^\s"\']+)["\']?\s*$')
# Changes whose blast radius we don't try to scope automatically.
_BROAD_PREFIXES = (
    "ansible/templates/",  # shared macros (traefik/networks/resources/...)
    "ansible/inventory/",  # host_vars / group_vars
    "ansible/roles/containers/common/",  # shared deploy path
    "ansible/deploy.yml",
    "ansible/filter_plugins/",  # toposort
    # Galaxy collections: installed by sops_setup (initial_setup.yml), NOT deploy.yml — a
    # bump here maps to no service, so without this it would silently ff-merge and sit
    # unapplied until a manual `initial_setup.yml --tags collections`. Defer-and-alert instead.
    "ansible/requirements.yml",
)
# The SOPS-encrypted secrets file. A change here maps to no service template, but the new
# value only reaches a container on its next deploy — so a secrets-ONLY push must NOT be
# silently fast-forwarded; the deployer defers-and-alerts (see gitops_deploy.py). NOT in
# _BROAD_PREFIXES on purpose: the /add-secret flow ships secrets.yml WITH the consuming
# template, and that should stay a scoped single-service deploy, not a manual full deploy.
_SECRETS_FILE = "ansible/vars/secrets.yml"


@dataclass
class ChangeSet:
    services: set[str] = field(default_factory=set)
    broad: bool = False
    secrets: bool = False


def services_from_changed_paths(paths: list[str]) -> ChangeSet:
    cs = ChangeSet()
    for p in paths:
        if p == _SECRETS_FILE:
            cs.secrets = True
            continue
        if any(p.startswith(prefix) for prefix in _BROAD_PREFIXES):
            cs.broad = True
            continue
        m = _ACTIVE_CONFIG.match(p)
        if m:
            cs.services.add(m.group(1))
    return cs


def next_action(
    local_head: str,
    origin_head: str,
    hold_sha: str | None,
    dirty: bool = False,
    origin_ahead: bool = True,
) -> str:
    # A dirty working tree (operator mid-edit) is a healthy skip, not an outage,
    # and must never be deployed from — so it short-circuits every other outcome.
    if dirty:
        return "dirty"
    if origin_head == local_head:
        return "noop"
    if hold_sha is not None and origin_head == hold_sha:
        return "skip_hold"
    # The deployer is pull-based and only fast-forwards, so it must act ONLY when
    # origin is strictly ahead of local. `origin_ahead=False` means origin is an
    # ancestor of local (the operator committed locally but hasn't pushed) or the
    # two diverged — either way there is nothing to fast-forward. Deploying here
    # would diff local..origin (the *reverse* of the un-pushed commits) and
    # mis-fire a redeploy + false rollback, so treat it as a no-op.
    if not origin_ahead:
        return "noop"
    return "deploy"


def should_alert_dirty(now, last_alert_date: str | None, alert_hour: int = 7) -> bool:
    """Whether this tick should send the dirty-working-tree Discord alert.

    The deploy timer fires every 30 min, so an unthrottled dirty alert pages the
    webhook through every long edit session. This caps it to at most once per
    calendar day and suppresses it before `alert_hour`, so an overnight-dirty
    tree pages once in the morning (~07:00 CT) instead of all night.

    `now` is the current time already in the target timezone (America/Chicago);
    `last_alert_date` is the ISO date (`YYYY-MM-DD`) we last alerted on, or None.
    The caller records `now.date().isoformat()` whenever this returns True.
    """
    if now.hour < alert_hour:
        return False
    return last_alert_date != now.date().isoformat()


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
    this host (e.g. dozzle is daniel-pi-only; the deployer runs on daniel-server).
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
