"""The `coredns` role's render: a one-key ConfigMap and a Corefile that keeps its three anchors (H1.4).

Flux resolves GHCR through the cluster DNS it owns, so a revision that leaves CoreDNS unable to
load its Corefile blocks its own fix. `reload` protects a running pod only, because a pod that
restarts with a bad Corefile crashloops. These checks parse the rendered Corefile for named
members and cannot prove that CoreDNS loads the file. `probe.py health coredns` and the
promote soak cover that half.

Run: uv run pytest ansible/roles/k8s/coredns/tests/test_coredns_render.py
"""

import re

from _k8s_render import host_context, render_role_template
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

_DEFAULTS = K8S_ROLES / "coredns" / "defaults" / "main.yml"
_FORWARD = re.compile(r"^\s*forward\s+\.\s+(?P<upstreams>[^{\n]+?)\s*\{", re.M)


def corefile_problems(corefile: str, pihole_first: str) -> list[str]:
    """Why a rendered Corefile is unfit, or an empty list.

    Args:
        corefile: the Corefile text.
        pihole_first: the address the `forward` line must list first.
    """
    problems = []
    found = _FORWARD.findall(corefile)
    if len(found) != 1:
        problems.append(f"want one `forward .` line, found {len(found)}")
    elif found[0].split()[0] != pihole_first:
        problems.append("the `forward` line does not list the Pi-hole address first")
    if "policy sequential" not in corefile:
        problems.append("`forward` lost `policy sequential`, so Pi-hole is a coin flip")
    if not re.search(r"^\s*kubernetes\s+cluster\.local\s", corefile, re.M):
        problems.append("the `kubernetes cluster.local` block is missing")
    if not re.search(r"^\s*reload\s*$", corefile, re.M):
        problems.append("the `reload` plugin is missing")
    return problems


def _rendered_corefile() -> tuple[str, str]:
    """The rendered Corefile, and the pihole-dns Service address it must forward to first."""
    cm = yaml_fast.safe_load(render_role_template("coredns", "configmap.yaml.j2"))
    first = yaml_fast.safe_load(_DEFAULTS.read_text())["coredns_upstreams"][0]
    assert first == "{{ dns_k8s_cluster_ip }}", (
        "the first upstream is the pihole-dns cluster IP"
    )
    return cm["data"]["Corefile"], host_context()["dns_k8s_cluster_ip"]


def test_the_configmap_carries_only_the_corefile_key():
    cm = yaml_fast.safe_load(render_role_template("coredns", "configmap.yaml.j2"))
    assert cm["kind"] == "ConfigMap"
    assert list(cm["data"]) == ["Corefile"], (
        "NodeHosts is written by k3s's node controller, so Flux must never declare it"
    )


def test_the_rendered_corefile_keeps_pihole_first_and_its_anchors():
    corefile, first = _rendered_corefile()
    assert corefile_problems(corefile, first) == []


def test_a_forward_line_that_lists_a_public_resolver_first_is_flagged():
    corefile, first = _rendered_corefile()
    swapped = _FORWARD.sub("forward . 1.1.1.1 1.0.0.1 {", corefile, count=1)
    assert any("Pi-hole address first" in p for p in corefile_problems(swapped, first))


def test_a_corefile_without_reload_or_the_cluster_block_is_flagged():
    corefile, first = _rendered_corefile()
    stripped = re.sub(r"^\s*reload\s*$", "", corefile, flags=re.M)
    stripped = stripped.replace("kubernetes cluster.local", "kubernetes example.test")
    got = corefile_problems(stripped, first)
    assert any(re.search("reload", p) for p in got)
    assert any(re.search(r"cluster\.local", p) for p in got)
