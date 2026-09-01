"""Notification-path checks for monitor-bridge — Discord webhook delivery and the SMTP backstop.

Slice 7 of the check.py split. Reads config as `cfg.X`, the fetch layer as `bridge_io.X` and
the shared streak counter as `bridge_streaks.X`, so the tests' patches on those modules reach
it; `discord_webhook_ok` is from-imported and patched on THIS module, where it is bound.
`_email_probe` lives here beside `email_backstop`, the only code that mutates it. Rule and
enforcement: bridge_config.py's header.
"""

import smtplib
import ssl
import time
import urllib.error

import bridge_config as cfg
import bridge_io
import bridge_streaks
from bridge_common import HTTP_TIMEOUT
from verdicts_service import discord_webhook_ok


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
