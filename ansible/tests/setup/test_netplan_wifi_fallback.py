"""Guard: each k3s node joins the WiFi as a fallback uplink that loses to its pinned ethernet.

WHY THIS EXISTS. The fallback works only while its routes sit behind the pinned link's metric
100 and ahead of nothing else, and while boot does not wait for it. A metric at or below 100
would move the node's traffic onto WiFi with the cable in, and a missing `optional: true` would
hold k3s at boot until WiFi associates. Both fail with nothing visibly broken.

The subject is the file each node's package installs: the k3s components'
`netplan-wifi-fallback.json.j2`, after homelab-secrets substitutes the SOPS values into it.
"""

import types

from lib.repo_paths import HOST_VARS, REPO
from lib import yaml_fast
from renderer.renderer_lib import host as host_mode

_WIFI_TEMPLATE = (
    "/usr/lib/homelab/secret-templates/etc/netplan/80-homelab-wifi-fallback.yaml"
)
_NODES = ("daniel-box", "daniel-server")
# The pinned link's route metric, from netplan-static-address.yaml.j2.
_PINNED_METRIC = 100


def _installed_wifi_file(host: str, ssid: str, password: str) -> str:
    """The wifi file `host`'s package installs, through homelab-secrets' own substitution."""
    package = host_mode.render_host(host)
    script = REPO / "hosts" / "lib" / "homelab-secrets"
    secrets = types.ModuleType("homelab_secrets")
    exec(compile(script.read_text(), str(script), "exec"), secrets.__dict__)
    (output,) = [o for o in package.outputs if "wifi_password" in o["keys"]]
    return secrets.substitute(
        package.files[_WIFI_TEMPLATE][0].decode(),
        set(output["keys"]),
        {"wifi_ssid": ssid, "wifi_password": password},
        output.get("format"),
    )


def wifi_fallback_problems(text: str, link: str, ssid: str, password: str) -> list[str]:
    """What keeps the rendered netplan file from making `link` a lower-priority uplink."""
    iface = (
        (yaml_fast.safe_load(text) or {}).get("network", {}).get("wifis", {}).get(link)
    )
    if iface is None:
        return [f"no wifis entry for {link}"]
    problems = []
    if iface.get("dhcp4") is not True:
        problems.append("dhcp4 is not true")
    if iface.get("optional") is not True:
        problems.append("optional is not true, so boot waits for WiFi")
    metric = (iface.get("dhcp4-overrides") or {}).get("route-metric")
    if not isinstance(metric, int) or metric <= _PINNED_METRIC:
        problems.append(f"route-metric is {metric}, not above {_PINNED_METRIC}")
    for block in ("dhcp4-overrides", "dhcp6-overrides", "ra-overrides"):
        if (iface.get(block) or {}).get("use-dns") is not False:
            problems.append(f"{block} lets the lease add resolvers")
    if iface.get("access-points") != {ssid: {"password": password}}:
        problems.append("access-points do not carry the SOPS SSID and password")
    return problems


def test_each_node_installs_a_fallback_behind_its_pin() -> None:
    # The sentinel carries the characters a JSON string must escape.
    ssid, password = "home: net", 'p"a#ss\\ word'
    for host in _NODES:
        link = yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text()).get(
            "k3s_node_wifi_link"
        )
        assert link, f"{host} sets no k3s_node_wifi_link"
        text = _installed_wifi_file(host, ssid, password)
        assert wifi_fallback_problems(text, link, ssid, password) == [], host


_FALLBACK = """
network:
  wifis:
    wlo1:
      dhcp4: true
      optional: true
      dhcp4-overrides: {route-metric: 600, use-dns: false}
      dhcp6-overrides: {use-dns: false}
      ra-overrides: {use-dns: false}
      access-points: {"home": {password: "pw"}}
"""


def test_a_fallback_behind_the_pin_is_clean() -> None:
    assert wifi_fallback_problems(_FALLBACK, "wlo1", "home", "pw") == []


def test_a_fallback_at_the_pins_metric_is_flagged() -> None:
    doc = _FALLBACK.replace("route-metric: 600", "route-metric: 100")
    assert wifi_fallback_problems(doc, "wlo1", "home", "pw") == [
        "route-metric is 100, not above 100"
    ]


def test_a_fallback_that_holds_boot_is_flagged() -> None:
    doc = _FALLBACK.replace("      optional: true\n", "")
    assert wifi_fallback_problems(doc, "wlo1", "home", "pw") == [
        "optional is not true, so boot waits for WiFi"
    ]
