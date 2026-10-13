#!/usr/bin/env python3
"""blackbox-exporter pins every ingress name to the ingress VIP, exactly as uptime-kuma does.

Both Deployments call `ingress_host_aliases()` (`ansible/templates/ingress-host-aliases.yml.j2`),
so the two lists cannot drift. The macro cannot stop a call from being lost: without it the
probes resolve `*.local.<domain>` through cluster DNS, the failure the pin exists to exclude.

Rendered with a real `hostvars`, because `rendered_docs()` leaves it empty and the macro's loop
would then yield only the one literal probe host.

Run: uv run pytest ansible/tests/services/test_blackbox_exporter_host_aliases.py
"""

from functools import cache

from _k8s_render import host_context, render_role_template
from lib import yaml_fast

PROBE_HOST_PREFIX = "edge-selfcheck.local."


@cache
def _host_aliases(role: str) -> list[dict]:
    ctx = host_context("daniel-box")
    hostvars = {ctx["k8s_primary_node"]: {"containers_list": ctx["containers_list"]}}
    doc = yaml_fast.safe_load(
        render_role_template(role, "deployment.yaml.j2", {"hostvars": hostvars})
    )
    return doc["spec"]["template"]["spec"].get("hostAliases") or []


def alias_problems(exporter: list[dict], kuma: list[dict]) -> list[str]:
    """Return why the exporter's hostAliases fail to match Kuma's pinned ingress names."""
    names = [name for alias in exporter for name in alias.get("hostnames", [])]
    problems = []
    if exporter != kuma:
        problems.append("hostAliases differ from uptime-kuma's")
    if not any(name.startswith(PROBE_HOST_PREFIX) for name in names):
        problems.append("the edge-selfcheck probe host is not pinned")
    if len(names) < 2:
        problems.append("no ingress name beyond the probe host is pinned")
    return problems


def test_the_exporter_pins_the_same_names_as_kuma_is_clean():
    assert (
        alias_problems(_host_aliases("blackbox-exporter"), _host_aliases("uptime-kuma"))
        == []
    )


def test_an_exporter_without_host_aliases_is_flagged():
    assert alias_problems([], _host_aliases("uptime-kuma")) == [
        "hostAliases differ from uptime-kuma's",
        "the edge-selfcheck probe host is not pinned",
        "no ingress name beyond the probe host is pinned",
    ]
