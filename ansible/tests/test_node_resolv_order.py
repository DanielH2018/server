"""Guard: the k3s node's resolv.conf must list Pi-hole FIRST, keep a fallback, and never rotate.

WHY THIS EXISTS. `k3s_node_dns_upstreams` is not a set of peers — the ORDER is the entire
mechanism. resolv.conf(5): "the resolver library queries them in the order listed", and the
`rotate` option is documented as spreading load "rather than having all clients try the first
listed server first every time". Leaving `rotate` unset is what makes the first entry the
preferred resolver on every query, which is what gives the node automatic failover to the
public resolver AND automatic return to Pi-hole with no daemon watching anything.

WHY IT NEEDS A GUARD RATHER THAN THE COMMENTS IT ALREADY HAS. Each of the three ways to break
it is silent. Swap the order and every lookup goes to Cloudflare while the file still lists
Pi-hole and the deploy still reads green. Drop the fallback and the node has no DNS at all
until the cluster is up — the exact cold-start deadlock slice-6 A1 was written to avoid. Add
`rotate` and roughly half the queries bypass Pi-hole, which is the same coin flip the cluster's
CoreDNS already had to be fixed for (`policy sequential` in coredns-corefile.j2). None of the
three produces an error, a failed task, or a red monitor.

The rendered file is checked, not just the variable, so a template that stops interpolating
the list cannot pass on the strength of correct defaults.
"""

from __future__ import annotations

import re

import yaml
from _helpers import ROLES as _ROLES

_DEFAULTS = _ROLES / "setup/k3s/defaults/main.yml"
_TEMPLATE = _ROLES / "setup/k3s/templates/resolv.conf.j2"

# The variable the Pi-hole VIP reaches this role by. Asserted as the literal Jinja reference
# rather than the address: pinning 10.0.0.243 here would pass while group_vars moved the VIP.
_VIP_REF = "{{ dns_k8s_vip }}"

_COMMENT = re.compile(r"^\s*#")


def nameserver_lines(text: str) -> list[str]:
    """The `nameserver` entries of a rendered resolv.conf, in file order, comments excluded."""
    return [
        ln.split(None, 1)[1].strip()
        for ln in text.splitlines()
        if not _COMMENT.match(ln) and ln.strip().startswith("nameserver ")
    ]


def options_line(text: str) -> str:
    """The `options` entry of a rendered resolv.conf, or an empty string when absent."""
    for ln in text.splitlines():
        if not _COMMENT.match(ln) and ln.strip().startswith("options "):
            return ln.strip()
    return ""


def order_problems(nameservers: list[str], options: str) -> list[str]:
    """Every way the resolver order can be silently wrong. Empty list means correct."""
    problems = []
    if not nameservers:
        problems.append("no nameserver lines at all")
    elif nameservers[0] != _VIP_REF:
        problems.append(f"first nameserver is {nameservers[0]!r}, not the Pi-hole VIP")
    if len(nameservers) < 2:
        problems.append("no fallback nameserver behind Pi-hole")
    if "rotate" in options.split():
        problems.append("`rotate` is set, which destroys the first-server preference")
    return problems


def test_the_live_defaults_and_template_keep_pihole_first() -> None:
    upstreams = yaml.safe_load(_DEFAULTS.read_text())["k3s_node_dns_upstreams"]
    rendered = _TEMPLATE.read_text().replace(
        "{% for server in k3s_node_dns_upstreams %}\nnameserver {{ server }}\n{% endfor %}",
        "".join(f"nameserver {s}\n" for s in upstreams),
    )
    problems = order_problems(nameserver_lines(rendered), options_line(rendered))
    assert not problems, (
        "The node's resolv.conf order is the whole failover mechanism (see "
        f"{_TEMPLATE.name}). Problems: " + "; ".join(problems)
    )


def test_pihole_first_with_a_fallback_is_clean() -> None:
    assert order_problems([_VIP_REF, "1.1.1.1"], "options timeout:2 attempts:1") == []


def test_the_fallback_listed_first_is_flagged() -> None:
    """The silent inversion: the file still names Pi-hole, and nothing ever asks it."""
    problems = order_problems(["1.1.1.1", _VIP_REF], "options timeout:2 attempts:1")
    assert problems == ["first nameserver is '1.1.1.1', not the Pi-hole VIP"]


def test_a_sole_pihole_entry_is_flagged() -> None:
    """No fallback re-creates the cold-start deadlock slice-6 A1 was written to avoid."""
    assert order_problems([_VIP_REF], "options timeout:2 attempts:1") == [
        "no fallback nameserver behind Pi-hole"
    ]


def test_rotate_is_flagged() -> None:
    """`rotate` looks like load-spreading and is really a coin flip past an ad-blocker."""
    problems = order_problems(
        [_VIP_REF, "1.1.1.1"], "options timeout:2 attempts:1 rotate"
    )
    assert problems == ["`rotate` is set, which destroys the first-server preference"]
