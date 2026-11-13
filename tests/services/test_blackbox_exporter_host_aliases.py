#!/usr/bin/env python3
"""blackbox-exporter pins every ingress name to the ingress VIP.

Its Deployment calls `ingress_host_aliases()` (`k8s/templates/ingress-host-aliases.yml.j2`).
The macro cannot stop the call from being lost: without it the probes resolve `*.local.<domain>` through cluster DNS, the failure the pin exists to exclude.

Rendered with the real `services`, because `rendered_docs()` leaves it empty and the macro's loop
would then yield only the one literal probe host.

Run: uv run pytest tests/services/test_blackbox_exporter_host_aliases.py
"""

from functools import cache

from _k8s_render import render_role_template
from lib import yaml_fast

PROBE_HOST_PREFIX = "edge-selfcheck.local."


@cache
def _host_aliases() -> list[dict]:
    doc = yaml_fast.safe_load(
        render_role_template("blackbox-exporter", "deployment.yaml.j2")
    )
    return doc["spec"]["template"]["spec"].get("hostAliases") or []


def alias_problems(exporter: list[dict]) -> list[str]:
    """Return why the exporter's hostAliases fail to pin the ingress names."""
    names = [name for alias in exporter for name in alias.get("hostnames", [])]
    problems = []
    if not any(name.startswith(PROBE_HOST_PREFIX) for name in names):
        problems.append("the edge-selfcheck probe host is not pinned")
    if len(names) < 2:
        problems.append("no ingress name beyond the probe host is pinned")
    return problems


def test_the_exporter_pins_the_ingress_names_is_clean():
    assert alias_problems(_host_aliases()) == []


def test_an_exporter_without_host_aliases_is_flagged():
    assert alias_problems([]) == [
        "the edge-selfcheck probe host is not pinned",
        "no ingress name beyond the probe host is pinned",
    ]
