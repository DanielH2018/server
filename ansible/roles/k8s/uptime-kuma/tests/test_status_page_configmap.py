"""The sync ConfigMap carries the id-to-name index, and carries nothing else from the Secret.

validate/k8s_manifests.py renders this template with `kuma_declarations` undefined, so its
index comes out empty and every structural check there passes over nothing. These render it
with declarations in hand, which is the only way the index path is exercised at all.
"""

import json
import sys as _sys
from pathlib import Path as _Path

import yaml

ROLE = _Path(__file__).resolve().parents[1]
REPO = ROLE.parents[3]
_sys.path.insert(0, str(REPO / "scripts"))

from validate.k8s_manifests import make_env, make_lookup, register_ansible_filters  # noqa: E402

SHARED_TEMPLATES = REPO / "ansible" / "templates"

DECLARATIONS = {
    "grafana-k8s": {
        "type": "http",
        "name": "k3s Grafana",
        "url": "https://grafana.example",
    },
    "daniel-pi-host": {"type": "ping", "name": "Daniel Pi Host"},
    "monitor-bridge-disk": {
        "type": "push",
        "name": "Root Disk",
        "push_token": "tok-must-not-leak",
    },
    "discord": {
        "type": "notification",
        "name": "Homelab Alerts",
        "config": {"discordWebhookUrl": "https://hook-must-not-leak"},
    },
}


def render():
    context = {
        "k8s_namespace": "homelab",
        "playbook_dir": str(REPO / "ansible"),
        "kuma_declarations": DECLARATIONS,
        "kuma_status_page_groups": [{"name": "Other", "match": [".*"]}],
    }
    env = make_env([ROLE / "templates", SHARED_TEMPLATES])
    env.globals["lookup"] = make_lookup(context)
    register_ansible_filters(env)
    text = env.get_template("status-page-sync-configmap.yaml.j2").render(context)
    return yaml.safe_load(text)


def test_the_index_maps_every_declared_monitor_to_its_display_name():
    index = json.loads(render()["data"]["index.json"])
    assert index == {
        "grafana-k8s": "k3s Grafana",
        "daniel-pi-host": "Daniel Pi Host",
        "monitor-bridge-disk": "Root Disk",
    }


def test_no_secret_value_reaches_the_configmap():
    rendered = yaml.safe_dump(render())
    assert "tok-must-not-leak" not in rendered
    assert "hook-must-not-leak" not in rendered


def test_the_rules_and_the_script_ship_beside_the_index():
    data = render()["data"]
    assert json.loads(data["rules.json"]) == [{"name": "Other", "match": [".*"]}]
    assert "def main(" in data["render_status_page.py"]
    compile(data["render_status_page.py"], "render_status_page.py", "exec")
    compile(data["push_heartbeat.py"], "push_heartbeat.py", "exec")
