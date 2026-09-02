"""The claude-otel query Services must keep an explicitly pinned ClusterIP.

Host-side tools quote these addresses as compile-time constants. `otelq` and
`otel-sweep` (chezmoi, `home/dot_local/bin/`) try the node-local hostPort first
and the ClusterIP second, which is what lets them keep working when a reboot
reschedules a backend onto the other node — the 2026-08-23 failure, where all
three Deployments moved to daniel-server and every loopback probe from
daniel-box read "unreachable" while telemetry was entirely healthy.

That fallback is only sound while the addresses cannot change under it. A
Service with no `clusterIP:` draws whatever the allocator hands out the next
time it is created, so dropping the pin does not fail anything here — it fails
later, on another machine, in a tool this repo does not contain. Hence a test.
"""

from __future__ import annotations

import ipaddress
import pathlib
import re

import pytest
import yaml
from _helpers import ALL_VARS, K8S_ROLES

ROLE = K8S_ROLES / "claude-otel"
DEFAULTS = ROLE / "defaults" / "main.yml"

# Service name -> the defaults variable its manifest must interpolate.
PINNED = {
    "loki": "claude_otel_loki_cluster_ip",
    "prometheus": "claude_otel_prometheus_cluster_ip",
    "tempo": "claude_otel_tempo_cluster_ip",
}

ALL_VARS_FILE = ALL_VARS

# Every pinned ClusterIP in the repo, with the file that owns it. The distinctness check below
# is only meaningful over the whole set: it used to read this role's three and pass while the
# other two lived in group_vars/all.yml, outside its scope (2026-08-23b review M10).
#
# Deliberately an explicit registry rather than a glob over `*_cluster_ip`. A glob also collects
# `pihole_k8s_dns_cluster_ip: "{{ dns_k8s_cluster_ip }}"` (pihole/defaults/main.yml:47), which is
# a deliberate alias whose raw value is a Jinja string, not an address — so the glob version of
# this fix false-positives on day one. Adding a sixth pin means adding a line here, which is the
# point: a new pin that collides with an existing one is exactly what this catches.
CROSS_ROLE_PINS = {
    "claude_otel_loki_cluster_ip": DEFAULTS,
    "claude_otel_prometheus_cluster_ip": DEFAULTS,
    "claude_otel_tempo_cluster_ip": DEFAULTS,
    "dns_k8s_cluster_ip": ALL_VARS_FILE,
    "k8s_registry_cluster_ip": ALL_VARS_FILE,
}

# k3s's default Service CIDR. An address outside it is not merely unconventional — the API
# server rejects it at apply time, so a typo like 10.0.0.158 for 10.43.0.158 passes an
# is_private check and fails only on the cluster (2026-08-23b review L10).
SERVICE_CIDR = ipaddress.ip_network("10.43.0.0/16")


@pytest.fixture(scope="module")
def defaults():
    return yaml.safe_load(DEFAULTS.read_text())


def service_spec(name):
    """The `spec:` block of the named Service, as raw template text.

    Parsed textually rather than with yaml: these are Jinja templates and the
    `{{ ... }}` values are not valid YAML scalars everywhere they appear.
    """
    text = (ROLE / "templates" / f"{name}.yaml.j2").read_text()
    for document in text.split("\n---\n"):
        if re.search(r"^kind: Service$", document, re.M) and re.search(
            rf"^  name: {name}$", document, re.M
        ):
            return document
    raise AssertionError(f"no Service named {name} in {name}.yaml.j2")


@pytest.mark.parametrize(("name", "variable"), sorted(PINNED.items()))
def test_service_pins_its_cluster_ip(name, variable):
    spec = service_spec(name)
    assert f"clusterIP: {{{{ {variable} }}}}" in spec, (
        f"the {name} Service must pin clusterIP to {variable}; without it the "
        "address is reassigned on recreate and otelq's fallback strands"
    )


def _pin_values() -> dict[str, ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every pinned ClusterIP in the repo, read from whichever file owns it."""
    by_file: dict[pathlib.Path, dict] = {}
    resolved = {}
    for variable, source in CROSS_ROLE_PINS.items():
        if source not in by_file:
            by_file[source] = yaml.safe_load(source.read_text())
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
