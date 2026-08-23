"""The claude-otel query Services must keep an explicitly pinned ClusterIP.

Host-side tools are to quote these addresses as compile-time constants. `otelq`
and `otel-sweep` (chezmoi, `home/dot_local/bin/`) are to try the node-local
hostPort first and the ClusterIP second, which is what lets them keep working
when a reboot reschedules a backend onto the other node — the 2026-08-23
failure, where all three Deployments moved to daniel-server and every loopback
probe from daniel-box read "unreachable" while telemetry was entirely healthy.
As of 2026-08-23 neither tool implements that fallback yet; the pin lands first
because the fallback cannot be written against an address that may move.

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

ROLE = pathlib.Path(__file__).resolve().parents[1] / "roles" / "k8s" / "claude-otel"
DEFAULTS = ROLE / "defaults" / "main.yml"

# Service name -> the defaults variable its manifest must interpolate.
PINNED = {
    "loki": "claude_otel_loki_cluster_ip",
    "prometheus": "claude_otel_prometheus_cluster_ip",
    "tempo": "claude_otel_tempo_cluster_ip",
}


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


@pytest.mark.parametrize("variable", sorted(PINNED.values()))
def test_pinned_value_is_a_private_address(defaults, variable):
    assert variable in defaults, f"{variable} must be defined in defaults/main.yml"
    address = ipaddress.ip_address(str(defaults[variable]))
    assert address.is_private, f"{variable} must be an RFC1918 address, got {address}"


def test_pinned_addresses_are_distinct(defaults):
    values = [defaults[v] for v in PINNED.values()]
    assert len(set(values)) == len(values), (
        f"two backends cannot share a ClusterIP: {values}"
    )
