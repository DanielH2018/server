"""The "Homelab Alerts" notification posts to the discord-relay sidecar, and its drops page (#5657).

Three files have to agree: the notification's webhookURL, the sidecar's port, and the
container name the loki-homelab rule `KumaNotificationDropped` selects.
"""

from _helpers import K8S_ROLES
from _kuma_entities import ROLE_DEFAULTS, _entities
from _k8s_render import rendered_docs
from lib import yaml_fast

RULES = K8S_ROLES / "loki-homelab/files/rules/homelab-logs.yaml"


def _relay_container() -> dict:
    deployments = [
        doc
        for role, _tpl, doc in rendered_docs()
        if role == "uptime-kuma" and doc.get("kind") == "Deployment"
    ]
    assert len(deployments) == 1, deployments
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    relays = [c for c in containers if c["name"] == "discord-relay"]
    assert relays, [c["name"] for c in containers]
    return relays[0]


def test_the_discord_notification_posts_to_the_relay_on_loopback():
    config = _entities()["discord.json"]["config"]
    port = ROLE_DEFAULTS["uptime_kuma_k8s_discord_relay_port"]
    assert config["webhookURL"] == f"http://127.0.0.1:{port}/"
    env = {e["name"]: e.get("value") for e in _relay_container()["env"]}
    assert env["RELAY_PORT"] == str(port)


def test_the_drop_rule_reads_the_relays_log():
    groups = yaml_fast.safe_load(RULES.read_text())["groups"]
    [rule] = [
        r
        for g in groups
        for r in g["rules"]
        if r.get("alert") == "KumaNotificationDropped"
    ]
    assert _relay_container()["name"] in rule["expr"], rule["expr"]
