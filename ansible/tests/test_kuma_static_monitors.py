"""Guards for the AutoKuma static entity files (slice-7 Phase D, KD7).

The static-monitors Secret is the alerting spine's declaration set after Kuma moves to the
cluster. Two silent failure modes get guards here:

- A monitor without a notification link is created and never pages (the macro's
  conditional-emission trap, now a per-file responsibility).
- A push monitor with retries > 0 flaps on a single missed cron beat. This file is now the
  SOLE guard of that rule: scripts/test_push_monitor_retries.py enforced it over the Docker
  compose templates, but that corpus reached zero push monitors on 2026-08-14 when the
  retired cloudflare-ddns role was archived (its two dead-men are declared here anyway), so
  the guard was vacuous and was removed.

Every entity also carries the fields AutoKuma v2.0.0 parses (`type` mandatory), and ids —
the filenames — must stay unique.
"""

import json
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

ANSIBLE = Path(__file__).resolve().parents[1]
TEMPLATE = ANSIBLE / "roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2"

STUBS = {
    "k8s_namespace": "homelab",
    "k8s_hostname_suffix": "-k8s",
    "domain": "example.com",
    "kuma_notification_id": "discord",
    "mqtt_k8s_vip": "10.0.0.242",
    "dns_k8s_vip": "10.0.0.243",
    "slzb_ip": "10.0.0.99",
    "email": "user@example.com",
    "monitor_discord_webhook_url": "https://discord.example/hook",
    "smtp_notify_app_password": "stub",
    "hostvars": {
        "daniel-pi": {"server_ip": "10.0.0.2"},
        "daniel-box": {
            "containers_list": [
                {"name": "freshrss", "bridge_probe_path": "/api/greader.php"},
                {"name": "livesync", "bridge_probe_path": "/"},
                {"name": "speedtest", "bridge_probe_path": "/api/healthcheck"},
            ]
        },
    },
    # Push tokens — every *_push_token the template references.
    "monitor_bridge_home_allowlist_push_token": "t" * 32,
    "docker_fleet_push_token": "t" * 32,
    "cloudflare_ddns_proxied_push_token": "t" * 32,
    "cloudflare_ddns_direct_push_token": "t" * 32,
    "longhorn_backup_push_token": "t" * 32,
    "daniel_box_disk_push_token": "t" * 32,
    "claude_otel_push_token": "t" * 32,
    "monitor_bridge_configarr_push_token": "t" * 32,
    "monitor_bridge_janitorr_push_token": "t" * 32,
    "monitor_bridge_fake_remux_push_token": "t" * 32,
    "monitor_bridge_fake_remux_replace_push_token": "t" * 32,
    "pi_sd_health_push_token": "t" * 32,
    "pi_recovery_push_token": "t" * 32,
    "secret_rotation_push_token": "t" * 32,
    # Both of these gate their monitor behind `{% if <token> %}`, so omitting the stub does not
    # fail a test — it renders the entity away and every guard below silently stops covering it.
    # Added 2026-08-21, when the email-tier guard was the first assertion to notice.
    "manifest_prune_push_token": "t" * 32,
    "etcd_snapshot_push_token": "t" * 32,
}

# The resend intervals come from the role's real defaults, not from a stub. Stubbing them would
# make every assertion below a statement about this file rather than about what deploys — the
# same "measured the wrong artifact" shape these guards exist to catch. A stubbed 360 would have
# reported a healthy resend on a monitor the role actually holds at 0.
ROLE_DEFAULTS = yaml.safe_load(
    (ANSIBLE / "roles/k8s/uptime-kuma/defaults/main.yml").read_text()
)


def _entities() -> dict[str, dict]:
    env = Environment(
        loader=FileSystemLoader([str(TEMPLATE.parent), str(ANSIBLE / "templates")]),
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    rendered = env.get_template(TEMPLATE.name).render(**{**ROLE_DEFAULTS, **STUBS})
    doc = yaml.safe_load(rendered)
    return {name: json.loads(body) for name, body in doc["stringData"].items()}


def test_every_entity_parses_and_declares_a_type():
    entities = _entities()
    assert entities, "no entities rendered — the Secret went empty"
    for name, entity in entities.items():
        assert name.endswith(".json"), (
            f"{name}: filename must carry the .json extension"
        )
        assert entity.get("type"), f"{name}: missing the mandatory `type` field"


def test_every_monitor_is_linked_to_the_discord_notification():
    # The silent-failure mode: an unlinked monitor is created and never pages. Notifications
    # themselves are the link's target, not carriers of one.
    for name, entity in _entities().items():
        if entity["type"] == "notification":
            continue
        assert "discord" in entity.get("notification_name_list", []), (
            f"{name}: monitor has no discord notification link — it would never page"
        )


def test_push_monitors_never_retry():
    # The only remaining enforcement of this rule (see the module docstring): a cron-fed push
    # monitor with retries > 0 turns one missed beat into interval*retries of silence
    # instead of a page.
    for name, entity in _entities().items():
        if entity["type"] != "push":
            continue
        retries = entity.get("max_retries", entity.get("maxretries"))
        assert retries == 0, (
            f"{name}: push monitor must set max_retries 0, got {retries}"
        )


# Monitors deliberately held at resendInterval 0, with the condition that lifts each hold. A
# hold is for a tile that is down, cannot recover without an event no operator controls, and
# would otherwise resend into a channel until it gets muted. Enumerated here rather than left to
# the template so that adding one is a visible decision and forgetting to remove one is a test
# that keeps naming it.
#
# Empty since 2026-08-21. `k3s Longhorn Backup` was the only entry, held from 2026-08-16 while
# the weekly tier had never completed a backup; its shard run completed at 04:30 on 2026-08-21
# and the hold lifted. An empty set is the normal state — it means every push monitor re-notifies.
RESEND_HELD: set[str] = set()


def test_push_monitors_re_notify_while_still_down():
    # Kuma's `resendInterval` default is 0, meaning "notify once on the down transition, then
    # never again". Every push monitor here ran that way until 2026-08-16: the Longhorn backup
    # tile went down at 04:30, sent one Discord message, and was silent for the rest of the day
    # while 11 backups stayed failed. The known instance was the GitOps tile; the actual scope
    # was all 48. Asserted for every push monitor so a new one cannot be added without it.
    for name, entity in _entities().items():
        if entity["type"] != "push":
            continue
        resend = entity.get("resendInterval")
        if entity.get("name") in RESEND_HELD:
            assert resend == 0, (
                f"{name} is in RESEND_HELD, so it must be held at 0 — a hold that drifts to a "
                f"non-zero value is worse than no hold, got {resend!r}"
            )
            continue
        assert isinstance(resend, int) and resend > 0, (
            f"{name}: push monitor needs a non-zero resendInterval or a sustained outage "
            f"pages exactly once, got {resend!r}"
        )


def test_both_managed_notifications_are_defined():
    entities = _entities()
    notifications = {n for n, e in entities.items() if e["type"] == "notification"}
    assert notifications == {"discord.json", "email.json"}


# Monitors whose failure is invisible on Discord alone and cannot wait for someone to notice a
# muted channel — the tier that also mails. Enumerated rather than pattern-matched: "has 'B2' in
# the name" is exactly the rule that put B2's headroom tile on this tier and left R2's off it
# until 2026-08-21, though both watch a Longhorn backup target's remaining free-tier capacity.
EMAIL_TIER = {
    "k3s Longhorn Backup",
    "Longhorn Volume Redundancy",
    "daniel-box Disk",
    "Daniel Pi SD Health",
    "Off-box etcd Snapshot",
    "Root Disk",
    "TLS Cert Expiry",
    "B2 Reachable",
    "B2 Free Tier Headroom",
    "R2 Free Tier Headroom",
    "SMART Data / Health",
    "UPS Battery Health",
    "Discord Delivery",
}


def test_email_tier_membership_is_exactly_declared():
    # Both directions matter. A monitor dropping off the tier loses the leg that survives a
    # degraded Discord; one drifting onto it dilutes an inbox that has to stay worth reading.
    named = {
        e["name"]
        for e in _entities().values()
        if e["type"] != "notification"
        and "email" in e.get("notification_name_list", [])
    }
    assert named == EMAIL_TIER
