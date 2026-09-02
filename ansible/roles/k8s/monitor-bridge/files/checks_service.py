"""Service checks for monitor-bridge.

Covers n8n, the *arrs, Bazarr, Prowlarr, the GitOps pair, the etcd restore drill, and the
Home Assistant heartbeat with its ip_ban arm.

Slice 7 of the check.py split. Reads config as `cfg.X`, the fetch layer as `bridge_io.X` and
the shared streak counter as `bridge_streaks.X`, so the tests' patches on those modules reach
it; the verdicts it from-imports from verdicts_service are patched on THIS module, where they
are bound. `_n8n_streaks` lives here beside `check_n8n`, the only code that mutates it. Rule
and enforcement: bridge_config.py's header.
"""

import os
import time
from datetime import datetime, timezone

import bridge_config as cfg
import bridge_io
import bridge_streaks
from bridge_common import sanitize
from bridge_parsing import parse_duration
from verdicts_service import (
    gitops_alive,
    ha_ban_verdict,
    ha_heartbeat_fresh,
    indexers_down,
    n8n_update_streaks,
    n8n_verdict,
    queue_warnings,
)


# Per-check mutable state. The thresholds these pair with moved to bridge_config.py; the
# counters stay beside the code that mutates them.
_n8n_streaks = {}


# checks: each returns (ok, msg)


def _parse_behind(marker):
    """Split the deployer's "<origin_sha> <unix_ts_first_seen>" marker.

    Returns (sha, since) with since=None when absent or unparseable — an unreadable marker must read
    as "not behind" rather than page forever on garbage.
    """
    if not marker:
        return "", None
    parts = marker.split()
    if len(parts) != 2:
        return "", None
    try:
        return parts[0], float(parts[1])
    except ValueError:
        return "", None


def gitops_status(
    hold_sha,
    diverged_sha=None,
    behind_since=None,
    now=None,
    max_behind_s=cfg.GITOPS_BEHIND_MAX_S,
    hold_plane=None,
):
    """Pure: is the deploy pipeline in a state needing operator action? Returns (ok, msg).

    Three down states share this monitor, most-specific first: a rolled-back commit HELD pending a
    revert, a local↔origin DIVERGENCE where the deployer can't fast-forward and silently noops
    forever while origin's new commits never deploy (2026-07-15 review L3), and the host simply
    sitting BEHIND origin for too long.

    Behind-ness is the general case the other two are specific instances of, and it is the one that
    caught nothing before: a deferred BROAD change never fast-forwards, so the host parks on an old
    tree while last_run keeps ticking (Alive green) and is_diverged stays false (origin is a strict
    descendant, so Status green too). daniel-server ran a 12-commit-old tree for hours that way on
    2026-08-02, all signals green, until un-deployed DNS records were noticed by hand.

    It is age-gated because being behind is normal in the small: a push is behind for one tick, and
    the dirty-tree path is behind for a whole edit session by design. Only sustained behind-ness is
    a fault. hold/diverged are still reported ahead of it — they name the actual cause, where
    "behind" only names the symptom.
    """
    if hold_sha:
        # A held BROAD apply is a different fault with a different fix. That arm is
        # forward-only: the tree is already fast-forwarded and a plane playbook failed
        # partway, so reverting the PR undoes nothing and the operator has to fix forward
        # and re-run. hold_sha still decides whether we page — hold_plane only says which
        # sentence to print, so a stale marker left by a cleared hold cannot page alone.
        if hold_plane:
            return False, (
                "broad apply held at %s — %s failed, plane unapplied; "
                "fix forward and re-run it" % (hold_sha[:8], hold_plane)
            )
        return False, "deploy held at %s — revert the offending PR" % hold_sha[:8]
    if diverged_sha:
        return False, (
            "local diverged from origin at %s — deployer can't fast-forward, new commits "
            "aren't deploying; reconcile the host tree" % diverged_sha[:8]
        )
    sha, since = _parse_behind(behind_since)
    if since is not None:
        age_s = (time.time() if now is None else now) - since
        if age_s > max_behind_s:
            return False, (
                "host %.0fh behind origin at %s (> %.0fh) — deploy deferred (broad change / "
                "dirty tree); run the manual deploy on the host"
                % (age_s / 3600, sha[:8], max_behind_s / 3600)
            )
    return True, "no held deploy"


def check_n8n():
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
    workflows = bridge_io._get_json(
        cfg.N8N_URL + "/api/v1/workflows?active=true&limit=250", headers=headers
    )
    executions = bridge_io._get_json(
        cfg.N8N_URL + "/api/v1/executions?status=error&limit=100", headers=headers
    )
    streaks = n8n_update_streaks(
        workflows,
        executions,
        _n8n_streaks,
        datetime.now(timezone.utc),
        parse_duration(cfg.N8N_FAIL_WINDOW),
    )
    return n8n_verdict(
        streaks, cfg.N8N_CONSECUTIVE_MAX, cfg.N8N_SYSTEMIC_STREAK, cfg.N8N_SYSTEMIC_MAX
    )


def check_arr_queue():
    """Sonarr/Radarr queue warning/blocked-import watchdog (see queue_warnings).

    Empty SONARR_API_KEY/RADARR_API_KEY independently skip that app (like the multi-webhook
    Discord check); both empty -> disabled (stays up), like check_n8n. An unreachable *arr
    API is NOT caught here — it bubbles up and _evaluate renders it `down` with the error,
    the same convention as check_n8n/check_scrutiny (a dead dependency pages; there's no
    shared root cause here the way Prometheus/exporter outages have, so nothing to gate).
    pageSize=250 mirrors n8n's page cap — ample for a homelab queue.
    """
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
        data = bridge_io._get_json(url, headers={"X-Api-Key": api_key})
        offenders.extend(queue_warnings(data, app_name))
    if offenders:
        desc = "; ".join(
            "[%s] %s — %s" % (app, sanitize(title), sanitize(reason))
            for app, title, reason in offenders[:5]
        )
        return False, "%d queue item(s) need review: %s" % (len(offenders), desc)
    return True, "queue clean (%s)" % ", ".join(a[0] for a in configured)


def bazarr_problems(status, health):
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


def check_bazarr():
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
    check_arr_queue/check_prowlarr_indexers convention. That covers the 401 a wrong key
    returns, which is itself the signal that Bazarr's API key in SOPS has gone stale.
    """
    if not cfg.BAZARR_API_KEY:
        return True, "bazarr monitoring disabled (no API key)"
    headers = {"X-API-KEY": cfg.BAZARR_API_KEY}
    status = bridge_io._get_json(cfg.BAZARR_URL + "/api/system/status", headers=headers)
    health = bridge_io._get_json(cfg.BAZARR_URL + "/api/system/health", headers=headers)
    problems = bazarr_problems(status, health)
    if problems:
        return False, "; ".join(problems[:5])
    versions = (status or {}).get("data") or {}
    return True, "bazarr ok (sonarr %s, radarr %s)" % (
        versions.get("sonarr_version") or "n/a",
        versions.get("radarr_version") or "n/a",
    )


def check_prowlarr_indexers():
    """Prowlarr sustained-indexer watchdog (see indexers_down):

    page only when an indexer has been failing >= PROWLARR_INDEXER_MIN_DOWN_MIN, not on the brief
    flaps public trackers throw that self-clear inside Prowlarr's backoff.

    Empty PROWLARR_API_KEY -> disabled (stays up), like check_n8n. An unreachable Prowlarr is NOT
    caught here — it bubbles up and _evaluate renders it `down` with the error (the
    check_arr_queue/check_n8n convention; the sustained-failure grace is about indexer flaps, not
    the bridge's own reach). The all-indexers-down red error stays with Prowlarr's own in-app
    onHealthIssue notification — this owns the per-indexer sustained signal Prowlarr can't express.
    """
    if not cfg.PROWLARR_API_KEY:
        return True, "prowlarr indexer monitoring disabled (no API key)"
    headers = {"X-Api-Key": cfg.PROWLARR_API_KEY}
    status = bridge_io._get_json(
        cfg.PROWLARR_URL + "/api/v1/indexerstatus", headers=headers
    )
    indexers = bridge_io._get_json(
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


def check_gitops_alive():
    """Checks that the GitOps deployer's last_run marker is fresh.

    Down when the marker is missing (the deployer never completed a tick) or unparseable.
    Returns (ok, msg).
    """
    try:
        with open(os.path.join(cfg.GITOPS_STATE_DIR, "last_run")) as fh:
            ts = float(fh.read().strip())
    except FileNotFoundError:
        return False, "no last_run marker (deployer never completed a tick?)"
    except ValueError:
        return False, "last_run marker unparseable"
    return gitops_alive(time.time() - ts, cfg.GITOPS_MAX_AGE_S)


def _read_gitops_marker(name):
    try:
        with open(os.path.join(cfg.GITOPS_STATE_DIR, name)) as fh:
            return fh.read().strip() or None
    except FileNotFoundError:
        return None


def check_gitops_status():
    return gitops_status(
        _read_gitops_marker("hold_sha"),
        _read_gitops_marker("diverged_sha"),
        _read_gitops_marker("behind_since"),
        hold_plane=_read_gitops_marker("hold_plane"),
    )


def check_etcd_restore_drill():
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

    age_s = time.time() - epoch
    if age_s > cfg.ETCD_DRILL_MAX_AGE_S:
        return (
            False,
            "etcd restore drill last passed %.1f days ago (weekly cadence)"
            % (age_s / 86400),
        )
    return True, "etcd restore drill passed %.1f days ago" % (age_s / 86400)


def with_ha_ban(ok, msg):
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
        banned = bridge_io.loki_count(cfg.HA_BAN_SELECTOR, cfg.HA_BAN_WINDOW)
    except Exception as e:
        return ok, "%s, ip_ban arm unavailable (%s)" % (msg, e)
    ban_ok, ban_msg = ha_ban_verdict(banned, cfg.HA_BAN_WINDOW)
    if ban_ok:
        return ok, "%s, %s" % (msg, ban_msg)
    return False, "%s | %s" % (ban_msg, msg)


def check_ha_heartbeat():
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
        state = bridge_io._get_json(
            cfg.HA_URL + "/api/states/" + cfg.HA_HEARTBEAT_ENTITY,
            headers={"Authorization": "Bearer " + cfg.HA_TOKEN},
        )
        ok, msg = ha_heartbeat_fresh(state, cfg.HA_HEARTBEAT_MAX_AGE_S)
    except (
        Exception
    ) as e:  # unreachable/auth -> route through the streak, don't page yet
        ok, msg = False, "HA API unreachable: %s" % e
    if ok:
        bridge_streaks._down_streaks["ha"] = 0
        return with_ha_ban(True, msg)
    bridge_streaks._down_streaks["ha"], ok, msg = bridge_streaks.down_streak(
        bridge_streaks._down_streaks.get("ha", 0),
        cfg.HA_CONSECUTIVE,
        msg,
        "deploy/restart grace",
    )
    return with_ha_ban(ok, msg)
