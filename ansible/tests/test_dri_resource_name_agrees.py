#!/usr/bin/env python3
"""The GPU extended-resource name is written out in three places that must agree.

`devic.es/dri` is what the dri-device-plugin DaemonSet advertises, what jellyfin and tdarr
request in their pod specs, and what monitor-bridge watches for deregistration. Those are three
independent string literals in three roles that happen to match — tdarr's comment calls the pair
a lockstep, which it is not (2026-08-23b review L7).

A single Ansible variable cannot cover all three: monitor-bridge's is a Python default inside a
container image, reachable only through `K8S_EXTENDED_RESOURCES`, which the env-secret does not
currently render. So the coupling gets a test instead of a var.

What goes wrong without it is quiet in both directions. Rename the plugin's resource and the
consumers' pods stay Pending with an unschedulable message naming a resource nobody grep'd for.
Rename it in a consumer only, and monitor-bridge keeps watching the old name — which its own
extended-resource arm reads as "advertised by no node", the fail-closed page recorded in that
role's CLAUDE.md as the 2026-08-20 false alarm.

Run: uv run pytest ansible/tests/test_dri_resource_name_agrees.py
"""

import re

from _helpers import K8S_ROLES, load_yaml

# role -> the defaults variable holding the resource name.
_CONSUMERS = {
    "jellyfin": "jellyfin_k8s_dri_resource",
    "tdarr": "tdarr_k8s_dri_resource",
}

_PLUGIN_DAEMONSET = K8S_ROLES / "dri-device-plugin" / "templates" / "daemonset.yaml.j2"
_BRIDGE_CHECK = K8S_ROLES / "monitor-bridge" / "files" / "check.py"


def _consumer_names() -> dict[str, str]:
    names = {}
    for role, variable in _CONSUMERS.items():
        defaults = load_yaml(K8S_ROLES / role / "defaults" / "main.yml")
        assert variable in defaults, (
            f"{variable} is gone from the {role} role's defaults"
        )
        names[role] = str(defaults[variable])
    return names


def test_every_consumer_requests_the_same_resource_name():
    names = _consumer_names()
    assert len(set(names.values())) == 1, (
        f"the GPU consumers disagree on the extended-resource name: {names}. A consumer "
        f"requesting a name no node advertises sits Pending, and the scheduler message is the "
        f"only place the mismatch appears."
    )


def test_the_bridge_watches_the_name_the_consumers_request():
    """monitor-bridge's default must track the consumers, or its extended-resource arm watches a
    resource nobody advertises and pages fail-closed — indistinguishable from a real wedge."""
    watched = re.search(
        r'_env\(\s*"K8S_EXTENDED_RESOURCES"\s*,\s*"([^"]+)"', _BRIDGE_CHECK.read_text()
    )
    assert watched, (
        f"could not find the K8S_EXTENDED_RESOURCES default in {_BRIDGE_CHECK}; if it moved, "
        f"point this test at the new home rather than deleting it."
    )
    consumer_name = next(iter(_consumer_names().values()))
    assert consumer_name in watched.group(1).split(","), (
        f"monitor-bridge watches {watched.group(1)!r}, which does not include the "
        f"{consumer_name!r} the GPU workloads request. Its arm would report that resource as "
        f"advertised by no node on a perfectly healthy cluster."
    )


def test_the_plugin_advertises_the_name_the_consumers_request():
    consumer_name = next(iter(_consumer_names().values()))
    assert consumer_name in _PLUGIN_DAEMONSET.read_text(), (
        f"the dri-device-plugin DaemonSet does not mention {consumer_name!r}. It is what makes "
        f"the resource exist; if it advertises something else, every consumer stays Pending."
    )
