#!/usr/bin/env python3
"""Scrutiny's Discord notify target: the env NAME, the shoutrrr transform, and the level trap.

Scrutiny had no notification target of its own until this guard's change (issue #1375) — a SMART
failure reached Discord only through monitor-bridge's poll of `/api/summary`. The push path is one
env var, and every way of getting it wrong lands green: the pod renders, boots, passes
`probe.py health scrutiny`, and delivers nothing. Three of those ways are checked here.

1. The NAME. Scrutiny's config keys are `SCRUTINY_` + the dotted config path, and notify sits at
   top-level `notify.urls` — `c.SetDefault("notify.urls", []string{})` in upstream's
   webapp/backend/pkg/config/config.go. The neighbouring influxdb keys are `web.influxdb.*`, so
   `SCRUTINY_WEB_NOTIFY_URLS` is the plausible wrong answer, and issue #1375 asked for exactly
   that spelling.
2. The TRANSFORM. Scrutiny speaks shoutrrr, so the raw `https://discord.com/api/webhooks/<id>/
   <token>` has to become `discord://<token>@<id>`. Getting the two halves the wrong way round
   renders and boots identically.
3. The LEVEL. `notify.level` is deprecated upstream and rejected at STARTUP with a
   ConfigValidationError — setting `SCRUTINY_NOTIFY_LEVEL` would crashloop the pod, not tune it.
   The level moved to the dashboard Settings page (SQLite in the config PVC); live value read
   2026-09-06 was `metrics.notify_level: 2` (Fail).

Run: uv run pytest ansible/tests/services/test_scrutiny_discord_notify.py
"""

import re

from _helpers import K8S_ROLES, jinja_env

ROLE = K8S_ROLES / "scrutiny"
SECRET_TPL = ROLE / "templates" / "secret.yaml.j2"
WEB_TPL = ROLE / "templates" / "web.yaml.j2"
TASKS = ROLE / "tasks" / "main.yml"

# A webhook-shaped value that is not one: same digits and letters, never a real credential.
FAKE_WEBHOOK = "https://discord.com/api/webhooks/1234567890/aaaaBBBBccccDDDD-_eeee"
FAKE_ID = "1234567890"
FAKE_TOKEN = "aaaaBBBBccccDDDD-_eeee"


def _render_secret(webhook: str) -> str:
    return (
        jinja_env()
        .from_string(SECRET_TPL.read_text())
        .render(
            monitor_discord_webhook_url=webhook,
            scrutiny_influxdb_admin_password="pw",
            scrutiny_influxdb_token="tok",
            k8s_namespace="homelab",
        )
    )


def _assert_regex() -> re.Pattern:
    """The shape check `tasks/main.yml` runs against the real webhook at deploy time."""
    text = TASKS.read_text()
    match = re.search(r"monitor_discord_webhook_url is match\('([^']+)'\)", text)
    assert match, (
        "no `monitor_discord_webhook_url is match(...)` assertion in "
        f"{TASKS} — the only check that sees the REAL webhook value is gone, so a "
        "malformed webhook would render a silently undeliverable discord:// URL"
    )
    return re.compile(match.group(1).replace("\\\\", "\\"))


def test_the_secret_carries_the_shoutrrr_form_of_the_webhook():
    rendered = _render_secret(FAKE_WEBHOOK)

    assert f'notify_url: "discord://{FAKE_TOKEN}@{FAKE_ID}"' in rendered, (
        "the Secret must carry the webhook as shoutrrr's discord://<token>@<id>, not the raw "
        f"https:// URL and not <id>@<token>. Rendered:\n{rendered}"
    )


def test_a_stubbed_secret_still_renders_valid_yaml():
    """The CI render guard stubs every SOPS value; a `.split()` on that must not abort."""
    rendered = _render_secret("STUB")

    assert "notify_url:" in rendered, (
        "the stubbed render dropped notify_url — validate/k8s_manifests renders this template "
        "with StubUndefined, so an expression that raises there fails the whole manifest gate"
    )


def test_the_deploy_time_shape_check_accepts_a_real_discord_webhook():
    assert _assert_regex().match(FAKE_WEBHOOK)


def test_the_deploy_time_shape_check_rejects_a_non_webhook():
    for bad in (
        "https://example.com/hook",
        "https://discord.com/api/webhooks/1234567890",
        "discord://token@1234567890",
        "",
    ):
        assert not _assert_regex().match(bad), (
            f"{bad!r} passed the webhook shape check — it would split into a discord:// URL "
            "scrutiny accepts at boot and never delivers to"
        )


def _web_manifest_lines() -> str:
    """web.yaml.j2 with its comment lines dropped.

    The comments name the wrong key deliberately, to say why it is wrong — a scan of the raw
    text would match the warning as if it were the bug.
    """
    return "\n".join(
        line
        for line in WEB_TPL.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_web_wrapper_exports_the_notify_url_under_the_name_scrutiny_reads():
    command = _web_manifest_lines()

    assert (
        'SCRUTINY_NOTIFY_URLS="$(cat /etc/scrutiny-secrets/notify_url)"' in command
    ), (
        "web.yaml.j2's wrapper must export SCRUTINY_NOTIFY_URLS from the Secret mount — an env: "
        "entry would put the webhook token in the pod spec, which is why the influxdb token is "
        "read the same way"
    )
    assert "SCRUTINY_WEB_NOTIFY_URLS" not in command, (
        "SCRUTINY_WEB_NOTIFY_URLS is the wrong key: notify lives at top-level `notify.urls`, not "
        "`web.notify.urls`. Scrutiny ignores an unknown env var, so this boots clean and notifies "
        "nothing"
    )


# The census this guard scans is found by glob, so a rename or a move would hand it an empty
# set and every assertion below would pass without running. Name the two templates that carry
# the notify wiring: a failure then says which one went missing, not that a count moved.
MUST_SCAN = frozenset({"secret.yaml.j2", "web.yaml.j2"})


def test_no_manifest_sets_the_deprecated_notify_level():
    templates = sorted(ROLE.rglob("*.j2"))
    missing = MUST_SCAN - {tpl.name for tpl in templates}
    assert not missing, (
        f"{sorted(missing)} is not under {ROLE}/templates any more, so this guard is scanning "
        "a set that no longer holds the notify wiring"
    )

    for tpl in templates:
        assert "SCRUTINY_NOTIFY_LEVEL" not in tpl.read_text(), (
            f"{tpl} sets SCRUTINY_NOTIFY_LEVEL. Upstream's ValidateConfig rejects `notify.level` "
            "with a ConfigValidationError at startup — the pod would crashloop. The level is a "
            "dashboard Setting (SQLite in the config PVC), not config"
        )
