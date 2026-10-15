"""Guard: each k3s node joins the WiFi as a fallback uplink that loses to its pinned ethernet.

WHY THIS EXISTS. The fallback works only while its routes sit behind the pinned link's metric
100 and ahead of nothing else, and while boot does not wait for it. A metric at or below 100
would move the node's traffic onto WiFi with the cable in, and a missing `optional: true` would
hold k3s at boot until WiFi associates. Both fail with nothing visibly broken.
"""

from lib.repo_paths import HOST_VARS, K3S_ROLE
from lib import yaml_fast
from _setup_render import render_setup_text

_TEMPLATE = "netplan-wifi-fallback.yaml.j2"
_DEST = "/etc/netplan/80-homelab-wifi-fallback.yaml"
_NODES = ("daniel-box", "daniel-server")
# The pinned link's route metric, from netplan-static-address.yaml.j2.
_PINNED_METRIC = 100


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


def test_each_node_renders_a_fallback_behind_its_pin() -> None:
    # The sentinel carries the characters a YAML scalar must quote.
    ssid, password = "home: net", 'p"a#ss: word'
    for host in _NODES:
        hv = yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text())
        link = hv.get("k3s_node_wifi_link")
        assert link, f"{host} sets no k3s_node_wifi_link"
        text = render_setup_text(
            "k3s",
            _TEMPLATE,
            {"k3s_node_wifi_link": link, "wifi_ssid": ssid, "wifi_password": password},
        )
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


def _tasks() -> list[dict]:
    return yaml_fast.safe_load((K3S_ROLE / "tasks" / "static-address.yml").read_text())


def test_the_fallback_file_is_written_without_logging_its_password() -> None:
    writes = [
        t
        for t in _tasks()
        if t.get("ansible.builtin.template", {}).get("dest") == _DEST
    ]
    assert len(writes) == 1
    assert writes[0].get("no_log") is True and writes[0].get("diff") is False


def test_emptying_the_wifi_link_removes_the_file_and_reapplies() -> None:
    tasks = _tasks()
    removal = [
        t
        for t in tasks
        if t.get("ansible.builtin.file", {}).get("path") == _DEST
        and t["ansible.builtin.file"].get("state") == "absent"
    ]
    assert len(removal) == 1, (
        "no task removes the fallback when k3s_node_wifi_link is empty"
    )
    assert removal[0]["when"] == "k3s_node_wifi_link | length == 0"
    flag = f"{removal[0]['register']}.changed"
    commands = [t for t in tasks if "ansible.builtin.command" in t]
    assert len(commands) == 2
    assert all(flag in t["when"] for t in commands), "removal does not trigger netplan"
