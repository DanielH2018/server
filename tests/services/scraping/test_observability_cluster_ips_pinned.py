"""The observability query Services must keep an explicitly pinned ClusterIP.

Host-side tools quote these addresses as compile-time constants. `otelq` and
`otel-sweep` (chezmoi, `home/dot_local/bin/`) try the node-local hostPort first
and the ClusterIP second, which is what lets them keep working when a reboot
reschedules a backend onto the other node, where every loopback probe from the
first node reads "unreachable" while telemetry is entirely healthy.

That fallback is only sound while the addresses cannot change under it. A
Service with no `clusterIP:` draws whatever the allocator hands out the next
time it is created, so dropping the pin does not fail anything here — it fails
later, on another machine, in a tool this repo does not contain. Hence a test.
"""

import ipaddress
import pathlib

import pytest
from lib import yaml_fast
from _helpers import K8S_ROLES
from lib.repo_paths import K8S_VARS
from _k8s_render import rendered_docs

ROLE = K8S_ROLES / "observability"
DEFAULTS = ROLE / "defaults" / "main.yml"

# Service name -> the defaults variable its manifest must interpolate.
PINNED = {
    "loki": "observability_loki_cluster_ip",
    "prometheus": "observability_prometheus_cluster_ip",
    "tempo": "observability_tempo_cluster_ip",
}

# An explicit registry, not a glob: a glob collects the `pihole_k8s_dns_cluster_ip` alias, a Jinja
# string.
CROSS_ROLE_PINS = {
    "observability_loki_cluster_ip": DEFAULTS,
    "observability_prometheus_cluster_ip": DEFAULTS,
    "observability_tempo_cluster_ip": DEFAULTS,
    "dns_k8s_cluster_ip": K8S_VARS,
    "k8s_registry_cluster_ip": K8S_VARS,
}

# An address outside k3s's Service CIDR is rejected by the API server at apply time.
SERVICE_CIDR = ipaddress.ip_network("10.43.0.0/16")


@pytest.fixture(scope="module")
def defaults():
    return yaml_fast.safe_load(DEFAULTS.read_text())


def rendered_service(name):
    """The named Service as the observability role renders it at daniel-box's inventory."""
    for role, _tpl, doc in rendered_docs():
        if (
            role == ROLE.name
            and doc.get("kind") == "Service"
            and (doc.get("metadata") or {}).get("name") == name
        ):
            return doc
    raise AssertionError(
        f"the observability render produced no Service named {name}, so its pin is unchecked"
    )


@pytest.mark.parametrize(("name", "variable"), sorted(PINNED.items()))
def test_service_pins_its_cluster_ip(name, variable, defaults):
    pinned = (rendered_service(name).get("spec") or {}).get("clusterIP")
    assert str(pinned) == str(defaults[variable]), (
        f"the rendered {name} Service has clusterIP {pinned!r}, not {variable} "
        f"({defaults[variable]}); without the pin the address is reassigned on recreate and "
        "otelq's fallback strands"
    )


def _pin_values() -> dict[str, ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every pinned ClusterIP in the repo, read from whichever file owns it."""
    by_file: dict[pathlib.Path, dict] = {}
    resolved = {}
    for variable, source in CROSS_ROLE_PINS.items():
        if source not in by_file:
            by_file[source] = yaml_fast.safe_load(source.read_text())
        content = by_file[source]
        assert variable in content, f"{variable} must be defined in {source}"
        resolved[variable] = ipaddress.ip_address(str(content[variable]))
    return resolved


@pytest.mark.parametrize("variable", sorted(CROSS_ROLE_PINS))
def test_pinned_value_is_inside_the_service_cidr(variable):
    address = _pin_values()[variable]
    assert address in SERVICE_CIDR, (
        f"{variable} is {address}, outside the Service CIDR {SERVICE_CIDR}. The API server "
        f"rejects such an address at apply time, so an is_private check alone lets a typo like "
        f"10.0.0.158 for 10.43.0.158 through every repo-side gate."
    )


def test_pinned_addresses_are_distinct():
    """Across every pinned ClusterIP in the repo, not just this role's three.

    Two Services with the same clusterIP is an admission rejection at deploy time, and the error
    names neither role — so catching it here is worth considerably more than catching it there.
    """
    values = _pin_values()
    duplicates = {
        address: sorted(name for name, value in values.items() if value == address)
        for address in set(values.values())
        if list(values.values()).count(address) > 1
    }
    assert not duplicates, f"two Services cannot share a ClusterIP: {duplicates}"
