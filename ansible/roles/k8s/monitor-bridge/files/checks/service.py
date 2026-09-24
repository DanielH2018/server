"""Service checks for monitor-bridge.

Covers n8n, the *arrs, Bazarr, Prowlarr, the etcd restore drill, and the Home Assistant
heartbeat with its ip_ban arm. The GitOps pair is `checks/gitops.py`: it grew past this
module's 600-line cap with the busy-lock arm (issue #1847), the same split idiom as
`checks/host_edge.py`.

Slice 7 of the check.py split. Reads config as `cfg.X`, the fetch layer as `bridge.net.X` and
the shared streak counter as `bridge.streaks.X`, so the tests' patches on those modules reach
it; the verdicts it from-imports from verdicts.service are patched on THIS module, where they
are bound. `_n8n_streaks` lives here beside `check_n8n`, the only code that mutates it. Rule
and enforcement: bridge/config.py's header.
"""

import os
import time
from datetime import datetime, timezone

from bridge.config import Config
import bridge.net
import bridge.streaks
from bridge.common import sanitize
from bridge.parsing import parse_duration
from verdicts.service import (
    ha_ban_verdict,
    ha_heartbeat_fresh,
    indexers_down,
    n8n_update_streaks,
    n8n_verdict,
    queue_warnings,
)


# Per-check mutable state. The thresholds these pair with moved to bridge/config.py; the
# counters stay beside the code that mutates them.
_n8n_streaks = {}


# checks: each returns (ok, msg)


def check_n8n(cfg: Config, now: datetime | None = None) -> tuple[bool, str]:
    """Consecutive failures of active ("Prod") n8n workflows (streak accumulated across cycles).

    Polls the n8n public API on the internal network (X-N8N-API-KEY header, no Authelia). n8n
    doesn't save successful executions, so the per-workflow failure streak lives in the
    module-global _n8n_streaks and is advanced by n8n_update_streaks each cycle; n8n_verdict
    turns it into the page decision. Empty N8N_API_KEY -> disabled (stays up) so it never
    false-pages before the operator sets the key. An unreachable/erroring API raises -> the loop
    renders it down with the error, like check_targets_down (a dead API surfaces, not silent-green).
    """
    if not cfg.N8N_API_KEY:
        return True, "n8n monitoring disabled (no API key)"
    headers = {"X-N8N-API-KEY": cfg.N8N_API_KEY}
    workflows = bridge.net._get_json(
        cfg.N8N_URL + "/api/v1/workflows?active=true&limit=250", headers=headers
    )
    executions = bridge.net._get_json(
        cfg.N8N_URL + "/api/v1/executions?status=error&limit=100", headers=headers
    )
    streaks = n8n_update_streaks(
        workflows,
        executions,
        _n8n_streaks,
        now if now is not None else datetime.now(timezone.utc),
        parse_duration(cfg.N8N_FAIL_WINDOW),
    )
    return n8n_verdict(
        streaks, cfg.N8N_CONSECUTIVE_MAX, cfg.N8N_SYSTEMIC_STREAK, cfg.N8N_SYSTEMIC_MAX
    )


def check_arr_queue(cfg: Config, fetch=None) -> tuple[bool, str]:
    """Sonarr/Radarr queue warning/blocked-import watchdog (see queue_warnings).

    Empty SONARR_API_KEY/RADARR_API_KEY independently skip that app (like the multi-webhook
    Discord check); both empty -> disabled (stays up), like check_n8n.

    **An unreachable *arr API is caught HERE, and that diverges from check_n8n/check_scrutiny
    on purpose.** Those let the error bubble to `_evaluate`, which renders it `down` with no
    grace. The *arrs are different in one respect that matters: they are Deployments this same
    bridge watches rolling, so their API refuses connections every time k3s replaces the pod —
    three of this monitor's DOWN episodes over the 30 days to 2026-09-11 were a fetch error
    co-timed with a `k8s_workloads ... radarr(1)` episode, which is a rollout being reported
    twice. `ARR_FETCH_CONSECUTIVE` holds `up` through that and pages on a *arr that stays
    unreachable. Do not restore the bubbling convention here without also removing that knob.

    The QUEUE verdict below is deliberately ungraced: a poisoned release sitting in the queue
    is not a transient, and delaying it is the 2026-07-01 incident this check exists for.
    pageSize=250 mirrors n8n's page cap — ample for a homelab queue.

    `fetch` is the injectable *arr boundary, the seam check_cluster_targets and
    checks/host_edge.py already use. Resolved in the body, not as a default: a default binds at
    import, before a test could reach bridge.net.
    """
    fetch = fetch or bridge.net._get_json
    apps = [
        (
            "Sonarr",
            cfg.SONARR_URL
            + "/api/v3/queue?includeUnknownSeriesItems=true&pageSize=250",
            cfg.SONARR_API_KEY,
        ),
        (
            "Radarr",
            # includeUnknownMovieItems is Radarr's spelling of Sonarr's
            # includeUnknownSeriesItems — both default FALSE, hiding exactly the unmapped/
            # poisoned-release queue items this check exists for (2026-07-01 incident class).
            cfg.RADARR_URL + "/api/v3/queue?includeUnknownMovieItems=true&pageSize=250",
            cfg.RADARR_API_KEY,
        ),
    ]
    configured = [a for a in apps if a[2]]
    if not configured:
        return True, "arr queue monitoring disabled (no API keys)"
    offenders = []
    for app_name, url, api_key in configured:
        try:
            data = fetch(url, headers={"X-Api-Key": api_key})
        except Exception as e:
            # The FETCH rides a streak; the queue verdict below does not. See the docstring.
            count, held, note = bridge.streaks.down_streak(
                bridge.streaks._down_streaks.get("arr_queue_fetch", 0),
                cfg.ARR_FETCH_CONSECUTIVE,
                "%s unreachable: %s" % (app_name, e),
                "rollout",
            )
            bridge.streaks._down_streaks["arr_queue_fetch"] = count
            return held, note
        offenders.extend(queue_warnings(data, app_name))
    bridge.streaks._down_streaks["arr_queue_fetch"] = 0
    if offenders:
        desc = "; ".join(
            "[%s] %s — %s" % (app, sanitize(title), sanitize(reason))
            for app, title, reason in offenders[:5]
        )
        return False, "%d queue item(s) need review: %s" % (len(offenders), desc)
    return True, "queue clean (%s)" % ", ".join(a[0] for a in configured)


def bazarr_problems(status: dict | None, health: dict | None) -> list[str]:
    """Problems from Bazarr's /api/system/status and /api/system/health payloads.

    Pure, so the reject case is testable without a live Bazarr.

    The peer-version fields are the interesting half. Bazarr fills `sonarr_version` /
    `radarr_version` by calling each app with ITS OWN stored copy of that app's API key, so a
    key Bazarr no longer holds correctly leaves the field empty while everything else about
    Bazarr still looks healthy. Measured against the live app 2026-08-29 after the keys were
    fixed: `sonarr_version='4.0.17.2952'`, `radarr_version='6.1.1.10360'`.

    An ABSENT field is not the same as an empty one and is deliberately ignored: Bazarr omits
    the key entirely when that integration is switched off, and alerting on a peer the operator
    turned off would page forever. Empty-but-present is the broken case.
    """
    problems = []
    data = (status or {}).get("data") or {}
    for peer in ("sonarr", "radarr"):
        field = "%s_version" % peer
        if field not in data:
            continue
        if not str(data.get(field) or "").strip():
            problems.append(
                "bazarr cannot reach %s (empty %s — stale API key in bazarr's own config?)"
                % (peer, field)
            )
    for item in (health or {}).get("data") or []:
        problems.append(
            "%s: %s" % (sanitize(item.get("object")), sanitize(item.get("issue")))
        )
    return problems


def check_bazarr(cfg: Config) -> tuple[bool, str]:
    """Bazarr's own health, and whether it can still talk to Sonarr and Radarr.

    Bazarr is the one *arr with no exporter, and that is why the 2026-08-29 stale-key incident
    surfaced only as an OOM 90 minutes later. Sonarr's and Radarr's own stale keys showed up
    immediately as failing exportarr scrapes; Bazarr had nothing watching it.

    NOT an exportarr sidecar, deliberately. exportarr does speak bazarr, but at the pinned
    v2.3.0 its collector always performs the full episode-subtitle walk — upstream measures
    that in "tens of seconds", spent inside Bazarr — and v2.3.0 predates the
    overlapping-collection skip that upstream added specifically to stop concurrent walks
    stacking (their issue #380, "bazarr CPU drainage"). Pointing that at the workload that had
    just OOM-killed would risk causing the failure this exists to detect. These two endpoints
    cost 477 and 13 bytes and measured 2-7 ms over three runs each, 2026-08-29.

    Empty BAZARR_API_KEY -> disabled (stays up), like check_n8n. An unreachable Bazarr is NOT
    caught here — it bubbles up and _evaluate renders it `down` with the error, the
    check_prowlarr_indexers/check_n8n convention (check_arr_queue LEFT that convention on
    2026-09-11 — see its docstring). That covers the 401 a wrong key
    returns, which is itself the signal that Bazarr's API key in SOPS has gone stale.
    """
    if not cfg.BAZARR_API_KEY:
        return True, "bazarr monitoring disabled (no API key)"
    headers = {"X-API-KEY": cfg.BAZARR_API_KEY}
    status = bridge.net._get_json(
        cfg.BAZARR_URL + "/api/system/status", headers=headers
    )
    health = bridge.net._get_json(
        cfg.BAZARR_URL + "/api/system/health", headers=headers
    )
    problems = bazarr_problems(status, health)
    if problems:
        return False, "; ".join(problems[:5])
    versions = (status or {}).get("data") or {}
    return True, "bazarr ok (sonarr %s, radarr %s)" % (
        versions.get("sonarr_version") or "n/a",
        versions.get("radarr_version") or "n/a",
    )


def check_prowlarr_indexers(cfg: Config) -> tuple[bool, str]:
    """Prowlarr sustained-indexer watchdog (see indexers_down).

    Pages only when an indexer has been failing >= PROWLARR_INDEXER_MIN_DOWN_MIN, not on the
    brief flaps public trackers throw that self-clear inside Prowlarr's backoff.

    Empty PROWLARR_API_KEY -> disabled (stays up), like check_n8n. An unreachable Prowlarr is NOT
    caught here — it bubbles up and _evaluate renders it `down` with the error (the
    check_n8n/check_bazarr convention; the sustained-failure grace is about indexer flaps, not
    the bridge's own reach). check_arr_queue no longer shares it — its *arrs are Deployments
    this bridge watches rolling, where a Prowlarr rollout is not a recurring source of pages. The all-indexers-down red error stays with Prowlarr's own in-app
    onHealthIssue notification — this owns the per-indexer sustained signal Prowlarr can't express.
    """
    if not cfg.PROWLARR_API_KEY:
        return True, "prowlarr indexer monitoring disabled (no API key)"
    headers = {"X-Api-Key": cfg.PROWLARR_API_KEY}
    status = bridge.net._get_json(
        cfg.PROWLARR_URL + "/api/v1/indexerstatus", headers=headers
    )
    indexers = bridge.net._get_json(
        cfg.PROWLARR_URL + "/api/v1/indexer", headers=headers
    )
    name_by_id = {i.get("id"): i.get("name") for i in indexers}
    offenders = indexers_down(
        status,
        name_by_id,
        datetime.now(timezone.utc),
        cfg.PROWLARR_INDEXER_MIN_DOWN_MIN,
        cfg.PROWLARR_INDEXER_IGNORE.split(","),
    )
    if offenders:
        desc = "; ".join("%s down %.0fm" % (sanitize(n), m) for n, m in offenders[:5])
        return False, "%d indexer(s) failing >=%gm: %s" % (
            len(offenders),
            cfg.PROWLARR_INDEXER_MIN_DOWN_MIN,
            desc,
        )
    return True, "all %d indexer(s) ok (none failing >=%gm)" % (
        len(name_by_id),
        cfg.PROWLARR_INDEXER_MIN_DOWN_MIN,
    )


def check_etcd_restore_drill(cfg: Config, now: float | None = None) -> tuple[bool, str]:
    """Is the off-box etcd snapshot still PROVABLY restorable?

    The snapshot half has been taken, uploaded and alarmed since 2026-08-16. Until 2026-08-28
    nothing watched the restore half: the drill wrote a stamp no code read, so a silently
    failing drill was indistinguishable from a passing one. etcd carries the Longhorn `Backup`
    CRs needed to FIND the volume backups, so this is the tier whose failure voids the rest of
    the recovery chain.

    Reads `last-success-list-only` SPECIFICALLY, never `last-success-full`. Only the list-only
    leg is scheduled — the full drill cannot pass on this host (five structural
    `k3s server --cluster-reset` failures documented in the drill's header) — so accepting either
    file would report the object-graph restore as proven when nothing here has ever proven it.
    That is the "one tier hiding behind another tier's evidence" shape, and the drill writes the
    mode into the stamp precisely so a reader cannot make that mistake.

    Fails closed on all three ways the input can be missing, and they are reported distinctly
    because they need different fixes:
      absent      the drill has never passed here — the state most worth reporting, and the one
                  `[[ -f $STAMP ]] && check_age` would have reported green
      unreadable  the stamp exists but this uid cannot read it. Real, not hypothetical: the
                  first run wrote 0640 root:root under UMASK 027 while this pod runs as uid
                  1000, and an unreadable file is otherwise indistinguishable from an absent one
      unparseable a stamp written by a future version whose format this cannot read
    """
    path = os.path.join(cfg.ETCD_DRILL_STATE_DIR, "last-success-list-only")
    try:
        with open(path) as fh:
            body = fh.read()
    except FileNotFoundError:
        return False, "no etcd restore drill has ever passed (no list-only stamp)"
    except PermissionError:
        return (
            False,
            "etcd drill stamp exists but is unreadable by this uid (needs 0644)",
        )
    except OSError as exc:
        return False, "cannot read the etcd drill stamp: %s" % exc

    epoch = None
    for line in body.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "epoch":
            try:
                epoch = float(value.strip())
            except ValueError:
                epoch = None
            break
    if epoch is None:
        return False, "etcd drill stamp has no readable epoch"

    age_s = (now if now is not None else time.time()) - epoch
    if age_s > cfg.ETCD_DRILL_MAX_AGE_S:
        return (
            False,
            "etcd restore drill last passed %.1f days ago (weekly cadence)"
            % (age_s / 86400),
        )
    return True, "etcd restore drill passed %.1f days ago" % (age_s / 86400)


def with_ha_ban(cfg: Config, ok: bool, msg: str) -> tuple[bool, str]:
    """Fold the ip_ban arm into a heartbeat verdict, ban winning the message.

    Folded into this monitor rather than given its own for the reason recorded at
    check_k8s_workloads' extended-resource arm: a new Kuma monitor costs a new push token in
    SOPS, and a ban is an HA fault, which is what this monitor already reports.

    # DECIDED: fails OPEN on a Loki error instead of adding ha_heartbeat to LOKI_DEPENDENT.
    # Membership there suppresses the WHOLE check during a Loki outage, which would blind the
    # real heartbeat — trading a live wedge-detector for a secondary arm is the wrong way round.
    # The ban arm also skips down_streak: down_streak exists to ride out a transient, and a ban is
    # a discrete event that either happened in the window or did not — a second cycle's confirmation
    # would add nothing. Note this arm reports the ban EVENT, not the ban STATE: it self-clears
    # HA_BAN_WINDOW after the ban is issued even though the entry survives in
    # /config/ip_bans.yaml. See the HA_BAN_WINDOW comment for why that is the only signal available.
    """
    try:
        banned = bridge.net.loki_count(cfg, cfg.HA_BAN_SELECTOR, cfg.HA_BAN_WINDOW)
    except Exception as e:
        return ok, "%s, ip_ban arm unavailable (%s)" % (msg, e)
    ban_ok, ban_msg = ha_ban_verdict(banned, cfg.HA_BAN_WINDOW)
    if ban_ok:
        return ok, "%s, %s" % (msg, ban_msg)
    return False, "%s | %s" % (ban_msg, msg)


def check_ha_heartbeat(cfg: Config, now: datetime | None = None) -> tuple[bool, str]:
    """Poll HA's automation-driven heartbeat over the apps network (Bearer token).

    Empty HA_URL/HA_TOKEN -> disabled (stays up), like check_n8n.

    Hysteresis (HA_CONSECUTIVE, like check_cpu_throttle): a planned redeploy takes HA's REST
    API unreachable for ~120s and then leaves the automation scheduler a beat behind, so a
    single cycle can read unreachable OR stale — a transient that should NOT page. Only the
    HA_CONSECUTIVE'th consecutive down cycle pushes `down`; earlier ones push `up` with a
    "streak n/N" msg, and one fresh read resets the streak. A genuinely wedged or auth-broken
    HA stays bad across cycles and still pages. The unreachable-API exception is caught HERE
    (not left to run_once) so the recreate-window connection error rides the same grace as
    staleness — both are the deploy, not a wedge.
    """
    if not cfg.HA_URL or not cfg.HA_TOKEN:
        return True, "HA heartbeat monitoring disabled (no URL/token)"
    try:
        state = bridge.net._get_json(
            cfg.HA_URL + "/api/states/" + cfg.HA_HEARTBEAT_ENTITY,
            headers={"Authorization": "Bearer " + cfg.HA_TOKEN},
        )
        ok, msg = ha_heartbeat_fresh(state, cfg.HA_HEARTBEAT_MAX_AGE_S, now=now)
    except (
        Exception
    ) as e:  # unreachable/auth -> route through the streak, don't page yet
        ok, msg = False, "HA API unreachable: %s" % e
    if ok:
        bridge.streaks._down_streaks["ha"] = 0
        return with_ha_ban(cfg, True, msg)
    bridge.streaks._down_streaks["ha"], ok, msg = bridge.streaks.down_streak(
        bridge.streaks._down_streaks.get("ha", 0),
        cfg.HA_CONSECUTIVE,
        msg,
        "deploy/restart grace",
    )
    return with_ha_ban(cfg, ok, msg)
