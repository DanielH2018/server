"""The rendered AutoKuma static entity set, shared by the guards over it.

Split out of `test_kuma_static_monitors.py` when that module crossed the 500-line cap, and
kept as a `_`-prefixed sibling for the same reason `ansible/tests/k8s/_manifest_guards.py` is
one: pytest prepends a test's own directory to `sys.path`, so both guard modules reach this by
bare name without it being collected as a test itself.
"""

import json

from lib import yaml_fast
from jinja2 import Environment, FileSystemLoader
from _helpers import ANSIBLE


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
    # The cluster's primary node. The template indexes `hostvars` by this rather than by a
    # literal hostname, so without it every hostvars lookup here resolves against Undefined.
    "k8s_primary_node": "daniel-box",
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
    "remember_logs_push_token": "t" * 32,
    "release_staleness_push_token": "t" * 32,
}

# The resend intervals come from the role's real defaults, not from a stub. Stubbing them would
# make every assertion below a statement about this file rather than about what deploys — the
# same "measured the wrong artifact" shape these guards exist to catch. A stubbed 360 would have
# reported a healthy resend on a monitor the role actually holds at 0.
ROLE_DEFAULTS = yaml_fast.safe_load(
    (ANSIBLE / "roles/k8s/uptime-kuma/defaults/main.yml").read_text()
)


def _entities() -> dict[str, dict]:
    env = Environment(
        loader=FileSystemLoader([str(TEMPLATE.parent), str(ANSIBLE / "templates")]),
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    rendered = env.get_template(TEMPLATE.name).render(**{**ROLE_DEFAULTS, **STUBS})
    doc = yaml_fast.safe_load(rendered)
    return {name: json.loads(body) for name, body in doc["stringData"].items()}
