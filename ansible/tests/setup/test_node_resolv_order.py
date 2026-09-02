"""Guard: each host's FIRST DNS hop must prefer Pi-hole, keep a fallback, and never spread load.

WHY THIS EXISTS. The upstream list is not a set of peers — the ORDER is the entire mechanism.
resolv.conf(5): "the resolver library queries them in the order listed", and the `rotate`
option is documented as spreading load "rather than having all clients try the first listed
server first every time". Leaving `rotate` unset is what makes the first entry the preferred
resolver on every query, which gives automatic failover to the public resolver AND automatic
return to Pi-hole with no daemon watching anything.

WHY IT NEEDS A GUARD RATHER THAN THE COMMENTS IT ALREADY HAS. Each of the three ways to break
it is silent. Swap the order and every lookup goes to Cloudflare while the config still names
Pi-hole and the deploy still reads green. Drop the fallback and the host has no DNS at all
until the cluster is up — the exact cold-start deadlock slice-6 A1 was written to avoid. Add
`rotate` and roughly half the queries bypass Pi-hole, which is the same coin flip the
cluster's CoreDNS already had to be fixed for (`policy sequential` in coredns-corefile.j2).
None of the three produces an error, a failed task, or a red monitor.

TWO HOSTS, TWO FIRST HOPS. daniel-pi resolves straight from resolv.conf, so its ordered list
is checked against the rendered file. daniel-box resolves through a local CoreDNS forwarder,
so its ordered list lives in a Corefile and is checked there. The invariant is the same; only
the file that carries it differs.

The rendered file is checked in both cases, not just the variable, so a template that stops
interpolating the list cannot pass on the strength of correct defaults.
"""

from __future__ import annotations

import re

import yaml
from _helpers import ROLES as _ROLES

_TEMPLATE = _ROLES / "setup/common/templates/resolv.conf.j2"

# Every host that resolves DIRECTLY from the shared template, and the variable each one
# passes. All are checked: the template is shared, so a correct file plus one bad caller is
# still a host resolving through Cloudflare.
#
# daniel-box IS NOT HERE, and that is deliberate. Since 2026-09-01 its resolv.conf names one
# entry, 127.0.0.1, and the ordered preference moved into its host forwarder's Corefile — it
# is checked at the bottom of this file instead. Adding it back here would fail on the
# sole-entry rule; LOOSENING THAT RULE TO ACCOMMODATE IT would delete the check that catches a
# missing fallback on the host that still needs one.
_CALLERS = {
    "daniel-pi": (
        _ROLES / "setup/optimize_pi/defaults/main.yml",
        "optimize_pi_dns_servers",
        "optimize_pi_dns_options",
    ),
}

# The variable the Pi-hole VIP reaches these roles by. Asserted as the literal Jinja reference
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
    template = _TEMPLATE.read_text()
    loop = (
        "{% for server in common_resolver_nameservers %}\n"
        "nameserver {{ server }}\n{% endfor %}"
    )
    assert loop in template, (
        f"{_TEMPLATE.name} no longer interpolates common_resolver_nameservers; this guard "
        "would silently stop checking the live values"
    )
    for host, (defaults, servers_var, options_var) in _CALLERS.items():
        values = yaml.safe_load(defaults.read_text())
        upstreams = values[servers_var]
        rendered = template.replace(
            loop, "".join(f"nameserver {s}\n" for s in upstreams)
        )
        # Substitute the options too. Reading the raw template here would leave the literal
        # `{{ common_resolver_options }}`, and the `rotate` half of this guard would be inert
        # against every caller — a check that can never fire.
        rendered = rendered.replace(
            "options {{ common_resolver_options }}", f"options {values[options_var]}"
        )
        problems = order_problems(nameserver_lines(rendered), options_line(rendered))
        assert not problems, (
            f"{host}'s resolv.conf order is the whole failover mechanism (see "
            f"{_TEMPLATE.name}, {servers_var}). Problems: " + "; ".join(problems)
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


# ── daniel-box: the first hop is the Corefile ───────────────────────────────────────────
# Same invariant, different file. CoreDNS's forward plugin defaults to `random`, so the list
# alone proves nothing — without `policy sequential` an ordered-looking list is a coin flip,
# which is the exact defect this arrangement replaced. Each way to break it is silent: invert
# the list and Cloudflare answers everything while the config still names Pi-hole; drop the
# public entries and a Pi-hole outage takes the node's DNS with it; set `max_fails 0` and
# CoreDNS stops health checking entirely while still reading as configured.

_K3S_DEFAULTS = _ROLES / "setup/k3s/defaults/main.yml"
_COREFILE = _ROLES / "setup/k3s/templates/host-corefile.j2"


def forward_problems(upstreams: list[str], corefile: str) -> list[str]:
    """Every way the host forwarder's preference can be silently wrong."""
    problems = []
    if not upstreams:
        problems.append("no forward upstreams at all")
    elif upstreams[0] != _VIP_REF:
        problems.append(f"first upstream is {upstreams[0]!r}, not the Pi-hole VIP")
    if len(upstreams) < 2:
        problems.append("no public fallback behind Pi-hole")
    if "policy sequential" not in corefile:
        problems.append(
            "`policy sequential` is missing, so forward load-balances at random"
        )
    if re.search(r"^\s*max_fails\s+0\s*$", corefile, re.M):
        problems.append("`max_fails 0` disables health checking entirely")
    return problems


def test_the_live_corefile_keeps_pihole_first() -> None:
    values = yaml.safe_load(_K3S_DEFAULTS.read_text())
    corefile = _COREFILE.read_text()
    loop = "{% for upstream in k3s_host_dns_upstreams %}{{ upstream }} {% endfor %}"
    assert loop in corefile, (
        f"{_COREFILE.name} no longer interpolates k3s_host_dns_upstreams; this guard would "
        "silently stop checking the live values"
    )
    rendered = corefile.replace(loop, " ".join(values["k3s_host_dns_upstreams"]))
    rendered = rendered.replace(
        "max_fails {{ k3s_host_dns_max_fails }}",
        f"max_fails {values['k3s_host_dns_max_fails']}",
    )
    problems = forward_problems(values["k3s_host_dns_upstreams"], rendered)
    assert not problems, (
        "daniel-box's forward order is the whole failover mechanism (see "
        f"{_COREFILE.name}, k3s_host_dns_upstreams). Problems: " + "; ".join(problems)
    )


def test_the_node_points_only_at_its_own_forwarder() -> None:
    """A second nameserver here makes a dead forwarder degrade silently to unfiltered DNS."""
    values = yaml.safe_load(_K3S_DEFAULTS.read_text())
    assert values["k3s_node_dns_upstreams"] == ["127.0.0.1"], (
        "daniel-box resolves through its host forwarder and nothing else; a fallback entry "
        "here turns a dead forwarder from a loud outage into silent unfiltered DNS"
    )


def test_a_sequential_forward_with_fallbacks_is_clean() -> None:
    assert (
        forward_problems([_VIP_REF, "1.1.1.1"], "policy sequential\nmax_fails 2\n")
        == []
    )


def test_a_random_policy_forward_is_flagged() -> None:
    """The default policy: an ordered-looking list that is really a coin flip."""
    assert forward_problems([_VIP_REF, "1.1.1.1"], "max_fails 2\n") == [
        "`policy sequential` is missing, so forward load-balances at random"
    ]


def test_an_inverted_forward_list_is_flagged() -> None:
    assert forward_problems(["1.1.1.1", _VIP_REF], "policy sequential\n") == [
        "first upstream is '1.1.1.1', not the Pi-hole VIP"
    ]


def test_max_fails_zero_is_flagged() -> None:
    """`max_fails 0` reads as configured and means never marked down, never health checked."""
    assert forward_problems(
        [_VIP_REF, "1.1.1.1"], "policy sequential\n    max_fails 0\n"
    ) == ["`max_fails 0` disables health checking entirely"]
