#!/usr/bin/env python3
"""The GPU extended-resource name is written out in three places that must agree.

`devic.es/dri` is what the dri-device-plugin DaemonSet advertises, what jellyfin and tdarr
request in their pod specs, and what monitor-bridge watches for deregistration. Those are three
independent string literals in three roles that happen to match — tdarr's comment calls the pair
a lockstep, which it is not.

A single Ansible variable cannot cover all three: monitor-bridge's is a Python default inside a
container image, reachable only through `K8S_EXTENDED_RESOURCES`, which the env-secret does not
currently render. So the coupling gets a test instead of a var.

What goes wrong without it is quiet in both directions. Rename the plugin's resource and the
consumers' pods stay Pending with an unschedulable message naming a resource nobody grep'd for.
Rename it in a consumer only, and monitor-bridge keeps watching the old name — which its own
extended-resource arm reads as "advertised by no node", the fail-closed page recorded in that
role's CLAUDE.md as a false alarm.

Every name is read from the rendered manifests, not from a role's defaults or a template's text:
a pod spec that stopped using its `*_dri_resource` variable, or a plugin whose `--device` config
changed while a comment still said `devic.es/dri`, would both pass a source scan.

Run: uv run pytest ansible/tests/services/test_dri_resource_name_agrees.py
"""

import re
from itertools import pairwise

from _helpers import K8S_ROLES
from _k8s_render import rendered_docs
from lib import yaml_fast

# The roles whose rendered Deployment must request the render node. Restated here so a consumer
# that stops requesting it fails loudly rather than dropping out of the comparison.
_CONSUMERS = ("jellyfin", "tdarr")

_PLUGIN_ROLE = "dri-device-plugin"
# generic-device-plugin's `--domain` default. The DaemonSet passes no `--domain`, and both nodes'
# allocatable list `devic.es/dri` (kubectl get nodes, 2026-10-01), which is how this was read.
_PLUGIN_DEFAULT_DOMAIN = "devic.es"

_BRIDGE_CHECK = K8S_ROLES / "monitor-bridge" / "files" / "bridge/config.py"


def _pod_spec(doc: dict) -> dict:
    return ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}


def extended_resources(doc: dict) -> set[str]:
    """Every domain-qualified resource a workload's containers set a limit on."""
    spec = _pod_spec(doc)
    return {
        name
        for container in (spec.get("containers") or [])
        + (spec.get("initContainers") or [])
        for name in ((container.get("resources") or {}).get("limits") or {})
        if "/" in name
    }


def advertised_resources(args: list[str]) -> set[str]:
    """The resource names a generic-device-plugin container advertises, from its args."""
    domain = _PLUGIN_DEFAULT_DOMAIN
    devices = []
    for flag, value in pairwise(args):
        if flag == "--domain":
            domain = value
        elif flag == "--device":
            devices.append(yaml_fast.safe_load(value)["name"])
    return {f"{domain}/{device}" for device in devices}


def test_advertised_resources_reads_the_device_name_and_honours_a_domain():
    device = "name: dri\ngroups:\n  - count: 4\n"
    assert advertised_resources(["--device", device]) == {"devic.es/dri"}
    assert advertised_resources(["--domain", "squat.ai", "--device", device]) == {
        "squat.ai/dri"
    }


def _consumer_names() -> dict[str, set[str]]:
    names: dict[str, set[str]] = {}
    for role, _tpl, doc in rendered_docs():
        if role in _CONSUMERS and doc.get("kind") == "Deployment":
            names.setdefault(role, set()).update(extended_resources(doc))
    for role in _CONSUMERS:
        assert names.get(role), (
            f"the rendered {role} Deployment requests no extended resource, so the GPU "
            f"comparison below would pass over nothing"
        )
    return names


def _consumer_name() -> str:
    (name,) = set().union(*_consumer_names().values())
    return name


def test_every_consumer_requests_the_same_resource_name():
    names = _consumer_names()
    assert len(set().union(*names.values())) == 1, (
        f"the GPU consumers disagree on the extended-resource name: {names}. A consumer "
        f"requesting a name no node advertises sits Pending, and the scheduler message is the "
        f"only place the mismatch appears."
    )


def _bridge_watched() -> str:
    """K8S_EXTENDED_RESOURCES as the pod sees it: the env-secret's value, else config.py's default.

    config.py is Python source shipped as a file, not a template, so its `_env` default has no
    rendered form; the rendered env-secret is checked first because it would override it.
    """
    for role, _tpl, doc in rendered_docs():
        if role == "monitor-bridge" and doc.get("kind") == "Secret":
            value = (doc.get("stringData") or {}).get("K8S_EXTENDED_RESOURCES")
            if value is not None:
                return str(value)
    watched = re.search(
        r'_env\(\s*"K8S_EXTENDED_RESOURCES"\s*,\s*"([^"]+)"', _BRIDGE_CHECK.read_text()
    )
    assert watched, (
        f"could not find the K8S_EXTENDED_RESOURCES default in {_BRIDGE_CHECK}; if it moved, "
        f"point this test at the new home rather than deleting it."
    )
    return watched.group(1)


def test_the_bridge_watches_the_name_the_consumers_request():
    """monitor-bridge's default must track the consumers, or its extended-resource arm watches a
    resource nobody advertises and pages fail-closed — indistinguishable from a real wedge."""
    watched = _bridge_watched()
    consumer_name = _consumer_name()
    assert consumer_name in [r.strip() for r in watched.split(",")], (
        f"monitor-bridge watches {watched!r}, which does not include the "
        f"{consumer_name!r} the GPU workloads request. Its arm would report that resource as "
        f"advertised by no node on a perfectly healthy cluster."
    )


def test_the_plugin_advertises_the_name_the_consumers_request():
    advertised: set[str] = set()
    for role, _tpl, doc in rendered_docs():
        if role == _PLUGIN_ROLE and doc.get("kind") == "DaemonSet":
            for container in _pod_spec(doc).get("containers") or []:
                advertised |= advertised_resources(container.get("args") or [])
    assert advertised, (
        f"the rendered {_PLUGIN_ROLE} DaemonSet passes no --device, so it advertises nothing"
    )
    consumer_name = _consumer_name()
    assert consumer_name in advertised, (
        f"the dri-device-plugin DaemonSet advertises {sorted(advertised)}, not the "
        f"{consumer_name!r} the GPU workloads request. It is what makes the resource exist; if "
        f"it advertises something else, every consumer stays Pending."
    )
