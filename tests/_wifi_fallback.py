"""A k3s node's installed WiFi fallback file, read from its rendered host package.

Both k3s components ship the same file, so daniel-box's and daniel-server's package tests read
it through this one helper.
"""

import types

from lib import yaml_fast
from lib.repo_paths import REPO

WIFI_FILE = "/etc/netplan/80-homelab-wifi-fallback.yaml"
WIFI_RENDERED = "/var/lib/homelab-secrets/netplan/80-homelab-wifi-fallback.yaml"


def installed_wifi_file(package, ssid: str, password: str) -> dict:
    """The installed wifi file as netplan parses it, through homelab-secrets' own substitution."""
    script = REPO / "hosts" / "lib" / "homelab-secrets"
    secrets = types.ModuleType("homelab_secrets")
    exec(compile(script.read_text(), str(script), "exec"), secrets.__dict__)
    (output,) = [o for o in package.outputs if "wifi_password" in o["keys"]]
    template = package.files[f"/usr/lib/homelab/secret-templates{WIFI_FILE}"][0]
    rendered = secrets.substitute(
        template.decode() if isinstance(template, bytes) else template,
        set(output["keys"]),
        {"wifi_ssid": ssid, "wifi_password": password},
        output.get("format"),
    )
    return yaml_fast.safe_load(rendered)
