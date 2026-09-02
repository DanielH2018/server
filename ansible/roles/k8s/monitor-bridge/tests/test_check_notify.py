"""Getting a verdict out: Discord, the SMTP backstop, and what a fetch failure says.

The backstop exists because Discord is a single point of failure for alerting, and the throttle
exists because the backstop is not. A message that says the wrong thing about why a fetch failed
is its own defect — an operator reading "down" for what was really "unreachable" chases the
wrong system.
"""

import io
import urllib.error
import urllib.request

import pytest

import bridge_config
import bridge_io
import bridge_parsing
import checks_notify
import email.message


def _http_error(url, status, msg):
    """An HTTPError as a consumer sees it once bridge_io has read and closed the body.

    These fakes bypass _get_json, which is what closes a real response, and an HTTPError
    left open is a ResourceWarning at GC that filterwarnings=error fails some later test on.
    """
    err = urllib.error.HTTPError(url, status, msg, email.message.Message(), None)
    err.close()
    return err


def test_discord_webhook_ok_200_is_up():
    ok, msg = checks_notify.discord_webhook_ok(200, "Homelab Alerts")
    assert ok
    assert "Homelab Alerts" in msg


def test_discord_webhook_404_is_down():
    ok, msg = checks_notify.discord_webhook_ok(404)
    assert not ok
    assert "404" in msg


def _discord_cycle(monkeypatch, status=200, raises=None):
    monkeypatch.setattr(
        bridge_config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc"
    )
    if raises is not None:

        def boom(*a, **k):
            raise raises

        monkeypatch.setattr(bridge_io, "_get_json", boom)
    elif status == 200:
        monkeypatch.setattr(
            bridge_io, "_get_json", lambda *a, **k: {"name": "Homelab Alerts"}
        )
    else:

        def http_err(*a, **k):
            raise _http_error("u", status, "err")

        monkeypatch.setattr(bridge_io, "_get_json", http_err)
    return checks_notify.check_discord()


def test_discord_single_failure_is_suppressed(monkeypatch):
    # One non-200 (a transient blip on the internet-facing check) must NOT page.
    ok, msg = _discord_cycle(monkeypatch, status=404)
    assert ok
    assert "1/2" in msg


def test_discord_two_consecutive_failures_alert(monkeypatch):
    # The 2nd straight failure is a genuinely dead webhook -> down.
    assert _discord_cycle(monkeypatch, status=404)[0]
    ok, msg = _discord_cycle(monkeypatch, status=404)
    assert not ok
    assert "404" in msg


def test_discord_valid_read_resets_streak(monkeypatch):
    assert _discord_cycle(monkeypatch, status=404)[0]  # streak 1
    ok, msg = _discord_cycle(monkeypatch, status=200)  # webhook recovered
    assert ok
    assert "valid" in msg
    ok, msg = _discord_cycle(monkeypatch, status=404)  # new streak, suppressed again
    assert ok
    assert "1/2" in msg


def test_discord_unreachable_rides_grace(monkeypatch):
    ok, msg = _discord_cycle(monkeypatch, raises=OSError("dns fail"))
    assert ok
    assert "1/2" in msg


def test_discord_disabled_without_url(monkeypatch):
    monkeypatch.setattr(bridge_config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(bridge_config, "DISCORD_CROWDSEC_WEBHOOK_URL", "")
    monkeypatch.setattr(bridge_config, "DISCORD_GITOPS_WEBHOOK_URL", "")
    ok, msg = checks_notify.check_discord()
    assert ok
    assert "disabled" in msg


def test_discord_verifies_all_configured_webhooks(monkeypatch):
    # All three webhooks valid -> up, naming each verified hop.
    monkeypatch.setattr(
        bridge_config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/kuma"
    )
    monkeypatch.setattr(
        bridge_config,
        "DISCORD_CROWDSEC_WEBHOOK_URL",
        "https://discord.com/api/webhooks/2/crowdsec",
    )
    monkeypatch.setattr(
        bridge_config,
        "DISCORD_GITOPS_WEBHOOK_URL",
        "https://discord.com/api/webhooks/3/gitops",
    )
    monkeypatch.setattr(
        bridge_io, "_get_json", lambda *a, **k: {"name": "Homelab Alerts"}
    )
    ok, msg = checks_notify.check_discord()
    assert ok
    assert "Kuma" in msg and "CrowdSec" in msg and "GitOps/Renovate" in msg


def test_discord_gitops_webhook_failure_pages(monkeypatch):
    # A revoked GitOps/Renovate webhook (delivers rollback + Renovate digests, whose "alive"
    # marker greens regardless of delivery — no Kuma backstop) pages, naming it, even though
    # Kuma's own webhook is fine.
    monkeypatch.setattr(
        bridge_config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/kuma"
    )
    monkeypatch.setattr(bridge_config, "DISCORD_CROWDSEC_WEBHOOK_URL", "")
    monkeypatch.setattr(
        bridge_config,
        "DISCORD_GITOPS_WEBHOOK_URL",
        "https://discord.com/api/webhooks/3/gitops",
    )

    def get(url, *a, **k):
        if "gitops" in url:
            raise _http_error(url, 404, "gone")
        return {"name": "Homelab Alerts"}

    monkeypatch.setattr(bridge_io, "_get_json", get)
    assert checks_notify.check_discord()[0]  # streak 1, suppressed
    ok, msg = checks_notify.check_discord()  # streak 2, pages
    assert not ok
    assert "GitOps/Renovate" in msg and "404" in msg


def test_discord_crowdsec_webhook_failure_pages(monkeypatch):
    # A revoked CrowdSec webhook (the one with no Kuma backstop) pages, naming it — even though
    # Kuma's own webhook is fine.
    monkeypatch.setattr(
        bridge_config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/kuma"
    )
    monkeypatch.setattr(
        bridge_config,
        "DISCORD_CROWDSEC_WEBHOOK_URL",
        "https://discord.com/api/webhooks/2/crowdsec",
    )

    def get(url, *a, **k):
        if "crowdsec" in url:
            raise _http_error(url, 404, "gone")
        return {"name": "Homelab Alerts"}

    monkeypatch.setattr(bridge_io, "_get_json", get)
    assert checks_notify.check_discord()[0]  # streak 1, suppressed
    ok, msg = checks_notify.check_discord()  # streak 2, pages
    assert not ok
    assert "CrowdSec" in msg and "404" in msg


def test_discord_healthchecks_webhook_failure_pages(monkeypatch):
    # A revoked healthchecks.io app webhook (its own check-down alerts, no Kuma backstop) pages,
    # naming it — even though Kuma's own webhook is fine.
    monkeypatch.setattr(
        bridge_config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/kuma"
    )
    monkeypatch.setattr(
        bridge_config,
        "DISCORD_HEALTHCHECKS_WEBHOOK_URL",
        "https://discord.com/api/webhooks/5/hc",
    )

    def get(url, *a, **k):
        if "/5/hc" in url:
            raise _http_error(url, 404, "gone")
        return {"name": "Homelab Alerts"}

    monkeypatch.setattr(bridge_io, "_get_json", get)
    assert checks_notify.check_discord()[0]  # streak 1, suppressed
    ok, msg = checks_notify.check_discord()  # streak 2, pages
    assert not ok
    assert "Healthchecks" in msg and "404" in msg


def test_email_backstop_disabled_without_password(monkeypatch):
    monkeypatch.setattr(bridge_config, "SMTP_PASSWORD", "")
    ok, msg = checks_notify.email_backstop()
    assert ok
    assert "disabled" in msg


def test_email_backstop_caches_success_within_interval(monkeypatch):
    monkeypatch.setattr(bridge_config, "SMTP_PASSWORD", "app-pw")
    monkeypatch.setattr(bridge_config, "EMAIL_PROBE_INTERVAL_S", 3600)
    monkeypatch.setattr(
        checks_notify, "_email_probe", {"ts": 0.0, "ok": True, "msg": ""}
    )
    calls = []

    def probe():
        calls.append(1)
        return True, "SMTP login ok"

    monkeypatch.setattr(checks_notify, "_smtp_login_ok", probe)
    assert checks_notify.email_backstop(now=10000.0)[0]  # stale ts -> probes
    ok, msg = checks_notify.email_backstop(now=11800.0)  # +1800 < interval -> cached
    assert ok and len(calls) == 1 and "verified" in msg
    checks_notify.email_backstop(now=13601.0)  # +3601 > interval -> re-probes
    assert len(calls) == 2


def test_email_backstop_failure_reprobes_every_cycle(monkeypatch):
    # a failure is NOT cached (unlike a success), so recovery is caught next cycle, not 6h later
    monkeypatch.setattr(bridge_config, "SMTP_PASSWORD", "app-pw")
    monkeypatch.setattr(bridge_config, "EMAIL_PROBE_INTERVAL_S", 3600)
    monkeypatch.setattr(
        checks_notify, "_email_probe", {"ts": 0.0, "ok": True, "msg": ""}
    )
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("auth refused")

    monkeypatch.setattr(checks_notify, "_smtp_login_ok", boom)
    ok, msg = checks_notify.email_backstop(now=10000.0)
    assert not ok and "FAILED" in msg
    ok, _ = checks_notify.email_backstop(
        now=10001.0
    )  # 1s later, well within interval -> still re-probes
    assert not ok and len(calls) == 2


def test_check_discord_email_backstop_failure_pages(monkeypatch):
    # webhooks fine but the email 2nd channel's SMTP login fails -> Discord Delivery pages after streak
    monkeypatch.setattr(
        bridge_config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/kuma"
    )
    monkeypatch.setattr(bridge_config, "DISCORD_CROWDSEC_WEBHOOK_URL", "")
    monkeypatch.setattr(bridge_config, "DISCORD_GITOPS_WEBHOOK_URL", "")
    monkeypatch.setattr(bridge_config, "DISCORD_ARR_WEBHOOK_URL", "")
    monkeypatch.setattr(bridge_config, "DISCORD_HEALTHCHECKS_WEBHOOK_URL", "")
    monkeypatch.setattr(bridge_config, "SMTP_PASSWORD", "app-pw")
    monkeypatch.setattr(
        checks_notify, "_email_probe", {"ts": 0.0, "ok": True, "msg": ""}
    )
    monkeypatch.setattr(
        bridge_io, "_get_json", lambda *a, **k: {"name": "Homelab Alerts"}
    )

    def boom():
        raise RuntimeError("auth refused")

    monkeypatch.setattr(checks_notify, "_smtp_login_ok", boom)
    assert checks_notify.check_discord()[0]  # streak 1, suppressed
    ok, msg = checks_notify.check_discord()  # streak 2, pages
    assert not ok
    assert "email backstop" in msg


#
# The 2026-08-02 B2 transaction-cap outage paged for 13h as "backup check error: timed out",
# which names neither the service nor the cause. These cover what the message must now carry —
# and, just as importantly, what it must never carry.


def test_endpoint_label_keeps_host_and_port():
    assert (
        bridge_parsing.endpoint_label("http://kopia:51515/api/v1/sources")
        == "kopia:51515"
    )


def test_endpoint_label_omits_the_path():
    """The Discord webhook probe goes through _get_json and its token lives in the PATH.

    Including the path would publish that token into the Kuma message and therefore into
    the very Discord channel it authenticates.
    """
    url = "https://discord.com/api/webhooks/123456789/s3cr3t-token-value"
    label = bridge_parsing.endpoint_label(url)
    assert label == "discord.com"
    assert "s3cr3t" not in label


def test_endpoint_label_omits_query_and_userinfo():
    assert "key" not in bridge_parsing.endpoint_label("http://h:1/p?api_key=key")
    assert bridge_parsing.endpoint_label("http://user:pw@h:1/p") == "h:1"


def test_endpoint_label_survives_a_junk_url():
    assert bridge_parsing.endpoint_label("") == "unknown host"


def test_describe_fetch_failure_names_the_endpoint():
    msg = bridge_parsing.describe_fetch_failure(
        "http://kopia:51515/api/v1/sources", TimeoutError("timed out")
    )
    assert msg == "kopia:51515: timed out"


def test_describe_fetch_failure_surfaces_the_server_body():
    """The body is where the real cause lives — urllib discards it unless read explicitly."""
    msg = bridge_parsing.describe_fetch_failure(
        "http://kopia:51515/api/v1/sources",
        "HTTP 500",
        "AccessDenied: Transaction cap exceeded, see the Caps & Alerts page",
    )
    assert "kopia:51515" in msg
    assert "Transaction cap exceeded" in msg


def test_describe_fetch_failure_collapses_whitespace_and_truncates():
    msg = bridge_parsing.describe_fetch_failure(
        "http://h:1/p", "HTTP 500", "a\n\n  b" + "c" * 500
    )
    assert "a b" in msg  # newlines collapsed — Kuma messages are single-line
    assert len(msg) < 260


def test_describe_fetch_failure_ignores_a_blank_body():
    msg = bridge_parsing.describe_fetch_failure("http://h:1/p", "boom", "   \n ")
    assert msg == "h:1: boom"


def test_get_json_attaches_the_error_body_to_httperror(monkeypatch):
    """The cap string only ever reaches an operator if the body is read off HTTPError.

    urllib exposes it as a one-shot file object that nothing reads by default, so the
    server's own explanation is discarded and the alert says only "HTTP Error 403".
    """

    body = b'{"error":"AccessDenied: Transaction cap exceeded, see Caps & Alerts"}'

    def boom(*_a, **_k):
        raise urllib.error.HTTPError(
            "http://kopia:51515/api/v1/sources",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(body),
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(urllib.error.HTTPError) as ei:
        bridge_io._get_json("http://kopia:51515/api/v1/sources")
    # Same type, and .code intact: check_discord branches on it to tell a revoked webhook
    # (decisive 404) from a transient network blip.
    assert ei.value.code == 403
    assert "kopia:51515" in str(ei.value)
    assert "Transaction cap exceeded" in str(ei.value)


def test_get_json_wraps_non_http_errors_without_leaking_the_url(monkeypatch):
    def boom(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    url = "https://discord.com/api/webhooks/123/s3cr3t-token"
    with pytest.raises(RuntimeError) as ei:
        bridge_io._get_json(url)
    assert "discord.com: timed out" == str(ei.value)
    assert "s3cr3t" not in str(ei.value)
