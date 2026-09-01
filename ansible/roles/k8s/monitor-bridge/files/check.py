#!/usr/bin/env python3
"""monitor-bridge — evaluate homelab health checks and push results to Uptime Kuma.

Stdlib only (runs on python:3.14-alpine with no extra deps). Each check returns
(ok: bool, msg: str) and maps to one Kuma *push* monitor. Every loop iteration pushes
the result (status=up|down): an explicit `down` gives fast, descriptive alerts, while
the Kuma push monitor's heartbeat interval is the backstop for "the bridge itself died"
(all pushes stop). Config is entirely env-driven so this file stays plain/testable.

Design: docs/superpowers/specs/2026-06-06-monitor-bridge-alerting-design.md
"""

import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# A name the test suite patches is read QUALIFIED from the module that binds it — `cfg.X`
# for every threshold and URL, `bridge_common.log`/`bridge_common.touch_heartbeat` — never
# from-imported. A from-import copies the value into this module's globals at import time,
# so a later `monkeypatch.setattr(bridge_config, "X", ...)` would change nothing this file
# reads and the test would pass against the real value. `_env`/`sanitize` and the verdicts
# are imported by name because the tests patch them HERE, on `check`, where they are read.
# Enforced by ansible/tests/test_bridge_patch_boundary.py; the census of what is patched
# where is ansible/tests/test_monitor_bridge_modules.py.
import bridge_common
from bridge_common import HTTP_TIMEOUT, _env, sanitize
from bridge_parsing import (
    parse_duration,
)
from verdicts_service import (
    discord_webhook_ok,
    gitops_alive,
    ha_ban_verdict,
    ha_heartbeat_fresh,
    indexers_down,
    n8n_update_streaks,
    n8n_verdict,
    queue_warnings,
)


import bridge_config as cfg
import bridge_io
import bridge_streaks
from checks_cluster import (
    check_cluster_prometheus,
    check_cluster_targets,
    check_cpu_throttle,
    check_k8s_workloads,
    check_oom,
    check_prometheus,
    check_restarts,
    check_targets_down,
    check_traefik_5xx,
    check_traefik_latency,
)
from checks_host import (
    check_cert,
    check_disk,
    check_host_temp,
    check_mem,
    check_pi_pressure,
    check_scrutiny,
    check_speedtest,
    check_ups,
)
from checks_storage import (
    check_b2_reachable,
    check_b2_storage,
    check_longhorn_volumes,
    check_pvc_fullness,
    check_r2_usage,
)
from checks_logs import (
    check_loki_ingestion,
    check_loki_reachable,
    check_promtail_dropped,
)


# Per-check mutable state. The thresholds these pair with moved to bridge_config.py; the
# counters stay beside the code that mutates them.
_n8n_streaks = {}


# checks: each returns (ok, msg)


def _parse_behind(marker):
    """Split the deployer's "<origin_sha> <unix_ts_first_seen>" marker. Returns (sha, since) with
    since=None when absent or unparseable — an unreadable marker must read as "not behind" rather
    than page forever on garbage."""
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
    """Prowlarr sustained-indexer watchdog (see indexers_down): page only when an indexer has been
    failing >= PROWLARR_INDEXER_MIN_DOWN_MIN, not on the brief flaps public trackers throw that
    self-clear inside Prowlarr's backoff.

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


def _discord_webhooks():
    """(label, url) pairs for each configured Discord webhook to verify (skips empties).

    Kuma's is the alert-chain delivery hop for every monitor; CrowdSec's is the independent
    security-ban delivery hop with no other backstop; GitOps/Renovate's carries the gitops-deploy
    rollback alert AND the renovate_notify digests (whose "alive" marker greens regardless of
    delivery); Arr's carries the *arr apps' own onHealthIssue alerts (direct POST from their
    in-app Discord Connect, config only in the app DBs); Healthchecks' is the healthchecks.io app's
    own check-down/up webhook (config only in hc.sqlite, a redundant secondary to its SMTP path).
    None has a Kuma backstop, so all five are verified together.
    """
    return [
        (label, url)
        for label, url in (
            ("Kuma", cfg.DISCORD_WEBHOOK_URL),
            ("CrowdSec", cfg.DISCORD_CROWDSEC_WEBHOOK_URL),
            ("GitOps/Renovate", cfg.DISCORD_GITOPS_WEBHOOK_URL),
            ("Arr", cfg.DISCORD_ARR_WEBHOOK_URL),
            ("Healthchecks", cfg.DISCORD_HEALTHCHECKS_WEBHOOK_URL),
        )
        if url
    ]


def _smtp_login_ok():
    """Connect to the SMTP server over implicit TLS and AUTH with the notify creds. (ok, msg).

    A revoked/expired Gmail app-password fails at login; a broken SMTP endpoint fails at connect. NOOP
    then QUIT — never sends a message. Raises are caught by the caller and ridden through the streak.
    """
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=HTTP_TIMEOUT, context=ctx
    ) as s:
        s.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
        s.noop()
    return True, "SMTP login ok (%s)" % cfg.SMTP_USER


_email_probe = {"ts": 0.0, "ok": True, "msg": "not yet probed"}


def email_backstop(now=None):
    """Throttled deliverability probe for the alert-email 2nd channel. (ok, msg).

    Empty SMTP_PASSWORD -> disabled (stays up). A SUCCESS is cached for EMAIL_PROBE_INTERVAL_S (so
    Gmail doesn't see an AUTH every cycle); a FAILURE isn't cached, so it re-probes every cycle until
    it recovers — and check_discord's DISCORD_CONSECUTIVE streak rides out a transient blip before
    paging. Module-global cache, reset on container restart, like the streak counters — no persistent
    state needed.
    """
    if not cfg.SMTP_PASSWORD:
        return True, "email backstop disabled (no SMTP password)"
    now = now if now is not None else time.time()
    if _email_probe["ok"] and now - _email_probe["ts"] < cfg.EMAIL_PROBE_INTERVAL_S:
        return True, "email backstop ok (verified %.1fh ago)" % (
            (now - _email_probe["ts"]) / 3600
        )
    try:
        ok, msg = _smtp_login_ok()
    except (
        Exception
    ) as e:  # revoked password / SMTP unreachable -> ride the check_discord streak
        ok, msg = False, "email backstop SMTP login FAILED: %s" % e
    if ok:
        _email_probe["ts"] = now
    _email_probe["ok"] = ok
    _email_probe["msg"] = msg
    return ok, msg


def check_discord():
    """GET-verify EVERY configured Discord notification webhook still delivers, plus the email backstop.

    Verifies the Kuma alert webhook, the CrowdSec ban-alert webhook, AND the GitOps/Renovate
    webhook (the latter two have no Kuma backstop). `down` if ANY is invalid, naming which. Each
    empty URL is skipped; all empty -> disabled (stays up), like
    check_n8n. Also probes the alert-email 2nd channel (email_backstop) — the independent delivery
    path this same monitor relies on when its Discord webhook is dead — so a silently revoked SMTP
    credential surfaces here too. Streak hysteresis (DISCORD_CONSECUTIVE, like check_ha_heartbeat):
    this check reaches the public internet (webhooks + SMTP), so a single transient non-200 / network
    blip pushes `up` with a streak msg and only the Nth straight failure pages — a genuinely dead
    webhook or SMTP credential stays bad and pages.
    """
    webhooks = _discord_webhooks()
    if not webhooks:
        return True, "Discord webhook check disabled (no URL)"
    ok, msg, valid = True, "", []
    for label, url in webhooks:
        try:
            data = bridge_io._get_json(url)
            w_ok, w_msg = discord_webhook_ok(200, (data or {}).get("name"))
        except urllib.error.HTTPError as e:
            w_ok, w_msg = discord_webhook_ok(e.code)
        except (
            Exception
        ) as e:  # network/DNS blip -> ride the streak, don't page on one cycle
            w_ok, w_msg = False, "unreachable: %s" % e
        if not w_ok:
            ok, msg = False, "%s webhook: %s" % (label, w_msg)
            break
        valid.append(label)
    if ok:
        e_ok, e_msg = email_backstop()
        if e_ok:
            valid.append("email")
        else:
            ok, msg = False, e_msg
    if ok:
        bridge_streaks._down_streaks["discord"] = 0
        return True, "delivery channels valid (%s)" % ", ".join(valid)
    bridge_streaks._down_streaks["discord"], ok, msg = bridge_streaks.down_streak(
        bridge_streaks._down_streaks.get("discord", 0),
        cfg.DISCORD_CONSECUTIVE,
        msg,
        "transient grace",
    )
    return ok, msg


CHECKS = [
    ("disk", _env("KUMA_PUSH_DISK", ""), check_disk),
    ("cert", _env("KUMA_PUSH_CERT", ""), check_cert),
    ("memory", _env("KUMA_PUSH_MEM", ""), check_mem),
    # restarts/oom/cpu RETARGETED 2026-08-14 (Phase G): retired with the Docker cadvisor
    # the same morning, re-armed the same evening against the kubernetes-cadvisor job's
    # label shape — grouped by pod (`name` is the runtime hash there). Same pure logic,
    # same thresholds; complements k8s_workloads' crashloop paging with OOM + sustained-
    # throttle depth the retirement dropped.
    ("restarts", _env("KUMA_PUSH_RESTARTS", ""), check_restarts),
    ("oom", _env("KUMA_PUSH_OOM", ""), check_oom),
    ("cpu", _env("KUMA_PUSH_CPU", ""), check_cpu_throttle),
    ("targets", _env("KUMA_PUSH_TARGETS", ""), check_targets_down),
    ("traefik5xx", _env("KUMA_PUSH_TRAEFIK", ""), check_traefik_5xx),
    (
        "traefik_latency",
        _env("KUMA_PUSH_TRAEFIK_LATENCY", ""),
        check_traefik_latency,
    ),
    ("n8n", _env("KUMA_PUSH_N8N", ""), check_n8n),
    ("arr_queue", _env("KUMA_PUSH_ARR_QUEUE", ""), check_arr_queue),
    ("bazarr", _env("KUMA_PUSH_BAZARR", ""), check_bazarr),
    (
        "prowlarr_indexers",
        _env("KUMA_PUSH_PROWLARR_INDEXERS", ""),
        check_prowlarr_indexers,
    ),
    ("gitops_alive", _env("KUMA_PUSH_GITOPS_ALIVE", ""), check_gitops_alive),
    ("gitops_status", _env("KUMA_PUSH_GITOPS_STATUS", ""), check_gitops_status),
    # Reads a stamp the drill writes weekly rather than a live source, so it is the same shape
    # as the gitops pair above: a hostPath the pod is pinned to, read fail-closed. Its token was
    # minted 2026-08-28, which is what let it be registered — test_checks_and_env_secret_push
    # _tokens_agree blocks a check whose KUMA_PUSH_* name has no env-secret entry, correctly:
    # such a check pushes to nowhere forever, present in the code and absent from the world.
    (
        "etcd_restore_drill",
        _env("KUMA_PUSH_ETCD_DRILL", ""),
        check_etcd_restore_drill,
    ),
    ("scrutiny", _env("KUMA_PUSH_SCRUTINY", ""), check_scrutiny),
    ("host_temp", _env("KUMA_PUSH_HOST_TEMP", ""), check_host_temp),
    ("ups", _env("KUMA_PUSH_UPS", ""), check_ups),
    ("pi_pressure", _env("KUMA_PUSH_PI", ""), check_pi_pressure),
    ("ha_heartbeat", _env("KUMA_PUSH_HA", ""), check_ha_heartbeat),
    ("speedtest", _env("KUMA_PUSH_SPEEDTEST", ""), check_speedtest),
    ("loki_ingestion", _env("KUMA_PUSH_LOKI", ""), check_loki_ingestion),
    (
        "promtail_dropped",
        _env("KUMA_PUSH_PROMTAIL_DROPPED", ""),
        check_promtail_dropped,
    ),
    ("discord", _env("KUMA_PUSH_DISCORD", ""), check_discord),
    ("r2_usage", _env("KUMA_PUSH_R2_USAGE", ""), check_r2_usage),
    ("b2_storage", _env("KUMA_PUSH_B2_STORAGE", ""), check_b2_storage),
    ("k8s_workloads", _env("KUMA_PUSH_K8S_WORKLOADS", ""), check_k8s_workloads),
    ("cluster_targets", _env("KUMA_PUSH_CLUSTER_TARGETS", ""), check_cluster_targets),
    (
        "longhorn_volumes",
        _env("KUMA_PUSH_LONGHORN_VOLUMES", ""),
        check_longhorn_volumes,
    ),
    ("pvc_fullness", _env("KUMA_PUSH_PVC", ""), check_pvc_fullness),
]

# Checks that query Prometheus. A single Prometheus outage would fail every one of them at once
# — one root cause, a storm of identical pages. run_once probes Prometheus first (check_prometheus
# -> its own monitor) and, when it's unreachable, SUPPRESSES these (pushes `up` with a skip msg so
# their push-monitor heartbeat stays alive and the dead-bridge watchdog isn't tripped) so only the
# Prometheus monitor pages. Keep this in sync with the prom_scalar/prom_vector callers above.
PROM_DEPENDENT = frozenset(
    {
        "disk",
        "cert",
        "memory",
        "restarts",
        "oom",
        "cpu",
        "targets",
        "traefik5xx",
        "ups",  # queries HA's Prometheus-scraped UPS battery sensors
        # Reads node_hwmon_temp_celsius. Its empty-vector branch pages on a blind hwmon
        # collector, so a Prometheus outage must suppress it — same reason as longhorn_volumes.
        "host_temp",
        "promtail_dropped",  # increase(promtail_dropped_entries_total) instant query
        # Reads longhorn_volume_robustness. Its own absent-metric branch pages when the
        # longhorn scrape job dies, so it must be suppressed when PROMETHEUS itself is the
        # cause — otherwise a Prometheus outage pages twice for one root cause.
        "longhorn_volumes",
    }
)

# One level BELOW the Prometheus gate: a single exporter down while Prometheus is UP fails every
# check reading its metrics at once. node-exporter death false-pages Root Disk + Memory (node_* go
# unavailable -> down) on top of the legitimate Scrape Targets page; cadvisor death makes
# restarts/oom/cpu read an empty vector -> silently green. Scrape Targets already names the dead
# `up{job=...}==0`, so run_once suppresses each dead exporter's dependents (pushes `up` with a skip
# msg, heartbeat kept alive) and lets Scrape Targets be the single page — the same
# one-root-cause-one-alert shape as the Prometheus gate, keyed by the Prometheus `job` label.
# Guarded by a test against CHECKS. (`cert`/`traefik5xx` read Traefik's own metrics, not these
# two exporters, so they're not mapped here.)
# host_temp joined 2026-08-29 with HWMON_TEMP_ORIGINS_MIN. Before the floor it had no per-host
# arm, so a single node-exporter death left it green and there was nothing to suppress. Now a
# dead node-exporter drops that host's hwmon series and trips the floor, so without this entry
# one root cause would page twice — Scrape Targets plus a coverage complaint naming the same
# host. Same reason disk and memory are here.
#
# The Pi scrapes under its OWN job — measured 2026-08-29, `count by (job, origin)
# (node_hwmon_temp_celsius)` returns job=node for daniel-server (12) and daniel-box (7) but
# job=node-pi for daniel-pi (2). This map is keyed by the Prometheus job, so a `node` entry alone
# suppresses two of the three hosts and the Pi's exporter death still double-pages. node-pi maps
# ONLY to host_temp: disk and memory exclude the Pi by origin (HOST_METRIC_ORIGIN_EXCLUDE, since
# check_pi_pressure owns them), so they have nothing to suppress there, while the hwmon floor
# counts all three hosts.
EXPORTER_DEPENDENT = {
    "node": frozenset({"disk", "memory", "host_temp"}),
    "node-pi": frozenset({"host_temp"}),
}

# Loki-reachability gate — the peer of the Prometheus gate for the Loki-querying checks. A single
# Loki outage makes loki_count raise in ALL of them at once (Loki Log Ingestion + Janitorr Errors)
# -> a 2-monitor storm for one root cause. run_once probes Loki first
# (check_loki_reachable -> its own "Loki Reachable" monitor) and, when it's unreachable, SUPPRESSES
# these (pushes `up` with a skip msg so their push heartbeats stay alive) so only Loki Reachable
# pages. Loki being UP but promtail not shipping is a different signal Loki Log Ingestion still
# surfaces (it evaluates whenever Loki is reachable). Guarded by a test against CHECKS.
LOKI_DEPENDENT = frozenset({"loki_ingestion"})

# B2-reachability gate — the third peer of the Prometheus and Loki gates (see check_b2_reachable /
# b2_reachable in run_once), and the fix for G2/G4 of docs/b2-transaction-cap-monitoring-gaps.md.
# It used to gate five kopia-era checks that read B2 health from state files written by periodic
# crons, so they reported the LAST SUCCESSFUL RUN rather than current health: on 2026-08-02 they
# read green through a nine-and-a-half-hour outage in which B2 refused every request. Those checks
# were removed 2026-08-10 — kopia is retired, backup moved to Longhorn (see
# docs/archive/k3s-migration/backup-consolidation-longhorn.md) — leaving this empty. b2_reachable itself
# stays: Longhorn still needs B2.
#
# b2_storage re-populated it on 2026-08-15. It queries B2 live rather than reading a cron's state
# file, so it does not have the stale-state fault the original five had — but it is gated for the
# other reason a gate exists: a transaction cap fails BOTH it and b2_reachable, and one root cause
# must not light two monitors.
B2_DEPENDENT = frozenset({"b2_storage"})

# Checks that read CLUSTER_PROM_URL rather than PROM_URL. Its own gate, not an arm of
# PROM_DEPENDENT, because a gate that is not watching a check's real source reports confidence it
# does not have.
#
# The two URLs used to name two instances on two hosts reached by two paths, and the Docker
# Prometheus being up said nothing about whether the cluster one was. Since the Docker plane
# retired (2026-08-14) PROMETHEUS_URL and CLUSTER_PROMETHEUS_URL render to the SAME cluster
# Service URL, so today both gates observe one instance and run_once reuses the prometheus gate's
# verdict here rather than probing twice. The split survives anyway, because it is what lets a
# second Prometheus be reintroduced without re-deciding which gate watches which check. So
# membership follows the URL a check reads, not which host happens to answer it.
#
# The division of labour with check_k8s_workloads' own fail-closed logic is deliberate and the two
# halves are not interchangeable. THIS gate covers "the cluster Prometheus is unreachable", which
# is a root cause that would otherwise page as a workload fault. The check's series-count floor
# covers "the cluster Prometheus is reachable but kube-state-metrics is not being scraped" — which
# this gate structurally cannot see, because the Prometheus answering `vector(1)` is perfectly
# healthy. Suppression is right for the first and would be dangerous for the second: it would turn
# a blind monitor green.
# pvc_fullness joins for the same reason and with the same division of labour: this gate covers
# "the cluster Prometheus is unreachable", while the check's own claim-count floor covers "the
# cluster Prometheus is answering but the kubelet volume stats are not being scraped". Its
# fail-closed arm pages on an empty vector, so a Prometheus outage must suppress it or one root
# cause lights two monitors.
#
# It is NOT given an EXPORTER_DEPENDENT entry keyed on job="kubernetes-kubelet", which is the
# nearest-looking wiring and would be wrong. Those claims are scraped under two jobs, so a dead
# kubelet job still leaves the apiserver job answering for 27 of the 43 claims — a PARTIAL
# blindness PVC_MIN_CLAIMS is sized to page on, and a job-keyed suppression would turn that page
# green. Same mistake as the `node`-only entry that suppressed two hosts of three for host_temp,
# not a fix for it.
CLUSTER_DEPENDENT = frozenset({"k8s_workloads", "cluster_targets", "pvc_fullness"})

# Reach-out checks that poll a live app dependency (n8n/sonarr/radarr/prowlarr/scrutiny/the Pi
# glances/the Cloudflare GraphQL API) with NO reachability gate above them and NO per-check
# hysteresis of their own — unlike
# check_ha_heartbeat/check_discord, whose HA_CONSECUTIVE/DISCORD_CONSECUTIVE grace rides out exactly
# this. On the bridge's first cycle after the weekly host reboot those dependencies are still
# starting, so an un-graced check flips its max_retries=0 monitor DOWN on that one transient cycle
# and pages (then recovers next cycle). run_once holds each of these `up` for the first
# GRACE_CYCLES-1 consecutive down cycles; the GRACE_CYCLES'th straight down still pages a
# genuinely-dead dependency. Must be DISJOINT from the run_once skip sets
# (PROM_DEPENDENT/LOKI_DEPENDENT/EXPORTER_DEPENDENT) so a graced check reaches the eval path every
# cycle. Guarded by a test against CHECKS: the "real check name" guard PLUS a completeness guard
# that every un-gated _get_json reach-out check is in here (prowlarr_indexers/scrutiny were added
# 2026-07-14 after they were found missing — the weekly-reboot flap's original set omitted them).
STARTUP_GRACE = frozenset(
    {
        "n8n",
        "arr_queue",
        "bazarr",
        "pi_pressure",
        "prowlarr_indexers",
        "scrutiny",
        "r2_usage",
        "speedtest",
    }
)

_grace_streaks = {}

# Which checks THIS instance runs. The Phase F twin/remnant split ended with the Docker
# uninstall (2026-08-14): the cluster deployment is now the ONLY bridge and runs every
# check (the gitops checks re-pointed at daniel-box's deployer via a hostPath — the pod
# is pinned there; disk_prune retired with the Docker daemon; pi_peers/renovate_alive
# became direct pushers at the host flips). The CHECKS_ONLY/CHECKS_SKIP mechanism stays
# — it is how any future split would be expressed, and the guards below keep it honest.
# CHECKS_ONLY (comma-separated names) enables exactly that set; CHECKS_SKIP drops
# names from whatever is otherwise enabled. The four reachability gates participate under
# the names their monitors push as (prometheus, loki_reachable, b2_reachable,
# cluster_prometheus). A filter that enables a gated check while disabling its gate would
# reintroduce the alert storm the gate exists to prevent, so main() refuses to start on one
# (validate_check_filter) — a crash-looping bridge is loud, a mis-gated one lies quietly.
GATE_DEPENDENTS = {
    "prometheus": PROM_DEPENDENT,
    "loki_reachable": LOKI_DEPENDENT,
    "b2_reachable": B2_DEPENDENT,
    "cluster_prometheus": CLUSTER_DEPENDENT,
}


def _name_set(value):
    return frozenset(n for n in value.replace(" ", "").split(",") if n)


CHECKS_ONLY = _name_set(_env("CHECKS_ONLY", ""))
CHECKS_SKIP = _name_set(_env("CHECKS_SKIP", ""))


def check_enabled(name, only=None, skip=None):
    only = CHECKS_ONLY if only is None else only
    skip = CHECKS_SKIP if skip is None else skip
    if only and name not in only:
        return False
    return name not in skip


def validate_check_filter(only, skip, checks):
    """Pure: return the list of problems with a CHECKS_ONLY/CHECKS_SKIP configuration."""
    known = {name for name, _, _ in checks} | set(GATE_DEPENDENTS)
    problems = ["unknown check name: %s" % n for n in sorted((only | skip) - known)]
    for gate, dependents in sorted(GATE_DEPENDENTS.items()):
        if check_enabled(gate, only, skip):
            continue
        enabled = sorted(d for d in dependents if check_enabled(d, only, skip))
        if enabled:
            problems.append(
                "gate %s is disabled but its dependents are enabled: %s"
                % (gate, ", ".join(enabled))
            )
    return problems


def apply_startup_grace(name, ok, msg, threshold, streaks):
    """Pure: hold a reach-out check `up` through the first `threshold`-1 consecutive down cycles.

    `streaks` is a name->consecutive-down-count dict, mutated in place. An `ok` result resets the
    count; a down result advances the shared `down_streak` hysteresis, so a held cycle reads with the
    same "down streak n/N" / "(n cycles)" wording as the HA/UPS/Discord per-check grace.
    """
    if ok:
        streaks[name] = 0
        return ok, msg
    streaks[name], ok, msg = bridge_streaks.down_streak(
        streaks.get(name, 0), threshold, msg, "startup/redeploy grace"
    )
    return ok, msg


def down_exporters(up_vector):
    """Pure: which EXPORTER_DEPENDENT jobs report up==0 in a Prometheus `up` vector.

    Fed prom_vector("up") — [(labels, value), ...]. Returns the subset of EXPORTER_DEPENDENT keys
    whose Prometheus job is down, so run_once can suppress their dependents. Unit-tested.
    """
    down_jobs = {m.get("job") for m, v in up_vector if v == 0}
    return {job for job in EXPORTER_DEPENDENT if job in down_jobs}


def _evaluate(name, fn):
    """Run one check; convert an unreachable source/metric into a descriptive `down` instead
    of letting it kill the loop. Returns (ok, msg)."""
    try:
        return fn()
    except Exception as e:  # an unreachable source/metric must not kill the loop
        return False, "%s check error: %s" % (name, e)


def _gate(name, fn, push_env):
    """Evaluate one reachability gate: verdict, log line, heartbeat push. Returns (ok, msg).

    A gate differs from an ordinary check only in what its verdict is used for — the CHECKS
    loop in run_once() reads it to suppress that gate's dependents, so a single outage pages
    once instead of storming. A disabled gate returns `True` so the filter suppresses nothing.
    """
    if not check_enabled(name):
        return True, "disabled by check filter"
    ok, msg = _evaluate(name, fn)
    bridge_common.log("OK  " if ok else "DOWN", name, "-", msg)
    bridge_io.push(_env(push_env, ""), ok, msg)
    return ok, msg


def run_once():
    # Prometheus reachability is evaluated FIRST and gates the prom-dependent checks: a single
    # Prometheus outage would otherwise page all of them at once (one root cause, an alert storm).
    # When it's down they're suppressed (pushed `up` with a skip msg, keeping each push monitor's
    # heartbeat alive) so only the Prometheus monitor pages; a real per-metric problem still alerts
    # whenever Prometheus is up.
    prom_ok, prom_msg = _gate("prometheus", check_prometheus, "KUMA_PUSH_PROMETHEUS")

    # Exporter-reachability gate (one level below the Prometheus gate): when Prometheus is up, probe
    # `up` once and suppress each dead exporter's dependents so a node-exporter/cadvisor death is one
    # page (Scrape Targets), not a 3-monitor false-page storm / silent-green split. A failure to
    # DETERMINE exporter health leaves `suppressed` empty (fail toward alerting, never masking).
    suppressed = set()
    if prom_ok and check_enabled("prometheus"):
        try:
            for job in down_exporters(
                bridge_io.prom_vector("up%s" % bridge_io.origin_sel())
            ):
                suppressed |= EXPORTER_DEPENDENT[job]
        except Exception as e:
            bridge_common.log("WARN: exporter-health probe failed:", e)

    # Loki-reachability gate (peer of the Prometheus gate): probe Loki once so a single Loki outage
    # is one page (Loki Reachable), not a storm across every Loki-querying check (LOKI_DEPENDENT).
    loki_ok, loki_msg = _gate(
        "loki_reachable", check_loki_reachable, "KUMA_PUSH_LOKI_REACHABLE"
    )

    # B2-reachability gate (peer of the two above): B2 caps TRANSACTIONS separately from storage
    # bytes, and the kopia-era state-file checks this used to gate all reported their last
    # successful cron run rather than current B2 health — the 2026-08-02 transaction-cap incident.
    # Those checks are gone (backup moved to Longhorn), but b2_reachable stays: Longhorn still
    # needs B2. The probe is throttled inside b2_reachable (it must not spend the transaction
    # budget it is watching), but the cached verdict is pushed every cycle so this monitor's own
    # heartbeat stays alive.
    b2_ok, b2_msg = _gate("b2_reachable", check_b2_reachable, "KUMA_PUSH_B2_REACHABLE")

    # Cluster-Prometheus gate (peer of the Prometheus gate, for the OTHER instance): the cluster
    # checks read daniel-box's Prometheus over the cluster ingress, a path none of the other gates
    # covers. Without this, a cluster ingress/Traefik outage would page as a workload fault rather
    # than as what it is.
    #
    # Since B5 that is usually the SAME instance the `prometheus` gate just probed — PROMETHEUS_URL
    # and CLUSTER_PROMETHEUS_URL both point at the cluster. Re-probing would spend a second request
    # on an answered question and, worse, light up two Kuma monitors for one fact, which reads as
    # more coverage than exists. So the verdict is reused when the URLs match, and only genuinely
    # separate endpoints get a separate probe and a separate page.
    #
    # DECIDED: this gate does NOT go through _gate() — the reuse branch below sits between the
    # check_enabled() test and the log/push, which is exactly the span _gate() owns. Threading a
    # precomputed verdict through would add a parameter for one caller and hide the reuse.
    cluster_ok, cluster_msg = True, "disabled by check filter"
    if check_enabled("cluster_prometheus"):
        # The same-instance reuse only holds when the prometheus gate actually probed.
        if (
            cfg.CLUSTER_PROM_URL
            and cfg.CLUSTER_PROM_URL == cfg.PROM_URL
            and check_enabled("prometheus")
        ):
            cluster_ok, cluster_msg = (
                prom_ok,
                "same instance as the Prometheus gate (%s)" % prom_msg,
            )
        else:
            cluster_ok, cluster_msg = _evaluate(
                "cluster_prometheus", check_cluster_prometheus
            )
        bridge_common.log(
            "OK  " if cluster_ok else "DOWN", "cluster_prometheus", "-", cluster_msg
        )
        bridge_io.push(
            _env("KUMA_PUSH_CLUSTER_PROMETHEUS", ""), cluster_ok, cluster_msg
        )

    for name, token, fn in CHECKS:
        if not check_enabled(name):
            continue
        if not prom_ok and name in PROM_DEPENDENT:
            ok, msg = True, "skipped — Prometheus unreachable (see Prometheus monitor)"
            bridge_common.log("SKIP", name, "-", msg)
        elif not loki_ok and name in LOKI_DEPENDENT:
            ok, msg = True, "skipped — Loki unreachable (see Loki Reachable monitor)"
            bridge_common.log("SKIP", name, "-", msg)
        elif not b2_ok and name in B2_DEPENDENT:
            ok, msg = True, "skipped — B2 unreachable (see B2 Reachable monitor)"
            bridge_common.log("SKIP", name, "-", msg)
        elif not cluster_ok and name in CLUSTER_DEPENDENT:
            ok, msg = (
                True,
                "skipped — cluster Prometheus unreachable (see Cluster Prometheus monitor)",
            )
            bridge_common.log("SKIP", name, "-", msg)
        elif name in suppressed:
            ok, msg = True, "skipped — exporter down (see Scrape Targets)"
            bridge_common.log("SKIP", name, "-", msg)
        else:
            ok, msg = _evaluate(name, fn)
            if name in STARTUP_GRACE:
                ok, msg = apply_startup_grace(
                    name, ok, msg, cfg.GRACE_CYCLES, _grace_streaks
                )
            bridge_common.log("OK  " if ok else "DOWN", name, "-", msg)
        bridge_io.push(token, ok, msg)


def main():
    once = "--once" in sys.argv
    problems = validate_check_filter(CHECKS_ONLY, CHECKS_SKIP, CHECKS)
    if problems:
        for p in problems:
            bridge_common.log("FATAL: bad CHECKS_ONLY/CHECKS_SKIP:", p)
        sys.exit(2)
    enabled = [name for name, _, _ in CHECKS if check_enabled(name)]
    bridge_common.log(
        "monitor-bridge starting (interval=%ss, once=%s, checks=%d/%d)"
        % (cfg.INTERVAL, once, len(enabled), len(CHECKS))
    )
    while True:
        run_once()
        bridge_common.touch_heartbeat(cfg.HEARTBEAT_FILE)
        if once:
            break
        time.sleep(cfg.INTERVAL)


if __name__ == "__main__":
    main()
