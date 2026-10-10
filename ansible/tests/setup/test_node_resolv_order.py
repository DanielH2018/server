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
until the cluster is up — a cold-start deadlock. Add
`rotate` and roughly half the queries bypass Pi-hole, which is the same coin flip the
cluster's CoreDNS already had to be fixed for (`policy sequential` in coredns-corefile.j2).
None of the three produces an error, a failed task, or a red monitor.

TWO HOSTS, TWO FIRST HOPS. daniel-pi resolves straight from resolv.conf, so its ordered list
is checked against the rendered file. daniel-box resolves through a local CoreDNS forwarder,
so its ordered list lives in a Corefile and is checked there. The invariant is the same; only
the file that carries it differs.

The rendered file is checked in both cases, not just the variable, so a template that stops
interpolating the list cannot pass on the strength of correct defaults. The render comes from
`_setup_render`, the harness `validate/setup_templates.py` renders these with. It used to come
from a hand-stitched substitution here — the loop body found by substring and replaced with
the caller's values — and that is a worse version of the same idea: it asserts the template
still spells the loop exactly as this module writes it, and the moment that stops matching the
guard checks a file with `{{ ... }}` still in it (#3202).
"""

import re

from _helpers import ROLES as _ROLES
from _setup_render import render_setup_text, rendered_setup_text, role_context
from lib.repo_paths import K3S_ROLE

_TEMPLATE = "resolv.conf.j2"

# Every host that resolves DIRECTLY from the shared template, and the variable each one
# passes. All are checked: the template is shared, so a correct file plus one bad caller is
# still a host resolving through Cloudflare.
#
# daniel-box IS NOT HERE, and that is deliberate. Its resolv.conf names one
# entry, 127.0.0.1, and the ordered preference lives in its host forwarder's Corefile — it
# is checked at the bottom of this file instead. Adding it back here would fail on the
# sole-entry rule; LOOSENING THAT RULE TO ACCOMMODATE IT would delete the check that catches a
# missing fallback on the host that still needs one.
_CALLERS = {
    "daniel-pi": (
        "optimize_pi",
        "optimize_pi_dns_servers",
        "optimize_pi_dns_options",
    ),
}


def _vip() -> str:
    """The Pi-hole VIP as the render resolves it, from group_vars rather than a literal.

    Pinning 10.0.0.243 here would pass while group_vars moved the VIP, and asserting the Jinja
    reference `{{ dns_k8s_vip }}` only holds while nobody renders the file (#3202).
    """
    return role_context(_ROLES / "setup" / "common")["dns_k8s_vip"]


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


def order_problems(nameservers: list[str], options: str, vip: str) -> list[str]:
    """Every way the resolver order can be silently wrong. Empty list means correct."""
    problems = []
    if not nameservers:
        problems.append("no nameserver lines at all")
    elif nameservers[0] != vip:
        problems.append(f"first nameserver is {nameservers[0]!r}, not the Pi-hole VIP")
    if len(nameservers) < 2:
        problems.append("no fallback nameserver behind Pi-hole")
    if "rotate" in options.split():
        problems.append("`rotate` is set, which destroys the first-server preference")
    return problems


def test_the_live_defaults_and_template_keep_pihole_first() -> None:
    vip = _vip()
    for host, (caller, servers_var, options_var) in _CALLERS.items():
        caller_vars = role_context(_ROLES / "setup" / caller)
        # The caller's own variables, resolved, passed to the SHARED template the way the
        # caller's tasks pass them. A template that stopped interpolating either one renders no
        # nameserver lines, which `order_problems` reports rather than passing over.
        rendered = render_setup_text(
            "common",
            _TEMPLATE,
            {
                "common_resolver_nameservers": caller_vars[servers_var],
                "common_resolver_options": caller_vars[options_var],
            },
        )
        problems = order_problems(
            nameserver_lines(rendered), options_line(rendered), vip
        )
        assert not problems, (
            f"{host}'s resolv.conf order is the whole failover mechanism (see "
            f"{_TEMPLATE}, {servers_var}). Problems: " + "; ".join(problems)
        )


def test_pihole_first_with_a_fallback_is_clean() -> None:
    assert (
        order_problems(
            ["10.0.0.243", "1.1.1.1"], "options timeout:2 attempts:1", "10.0.0.243"
        )
        == []
    )


def test_the_fallback_listed_first_is_flagged() -> None:
    """The silent inversion: the file still names Pi-hole, and nothing ever asks it."""
    problems = order_problems(
        ["1.1.1.1", "10.0.0.243"], "options timeout:2 attempts:1", "10.0.0.243"
    )
    assert problems == ["first nameserver is '1.1.1.1', not the Pi-hole VIP"]


def test_a_sole_pihole_entry_is_flagged() -> None:
    """No fallback re-creates the cold-start deadlock."""
    assert order_problems(
        ["10.0.0.243"], "options timeout:2 attempts:1", "10.0.0.243"
    ) == ["no fallback nameserver behind Pi-hole"]


def test_rotate_is_flagged() -> None:
    """`rotate` looks like load-spreading and is really a coin flip past an ad-blocker."""
    problems = order_problems(
        ["10.0.0.243", "1.1.1.1"], "options timeout:2 attempts:1 rotate", "10.0.0.243"
    )
    assert problems == ["`rotate` is set, which destroys the first-server preference"]


# ── daniel-box: the first hop is the Corefile ───────────────────────────────────────────
# Same invariant, different file. CoreDNS's forward plugin defaults to `random`, so the list
# alone proves nothing — without `policy sequential` an ordered-looking list is a coin flip,
# which is the exact defect this arrangement avoids. Each way to break it is silent: invert
# the list and Cloudflare answers everything while the config still names Pi-hole; drop the
# public entries and a Pi-hole outage takes the node's DNS with it; set `max_fails 0` and
# CoreDNS stops health checking entirely while still reading as configured.

_COREFILE = "host-corefile.j2"
_FORWARD = re.compile(r"^\s*forward\s+\.\s+(.+?)\s*\{\s*$", re.M)


def forward_upstreams(corefile: str) -> list[str]:
    """The upstream addresses a rendered Corefile's `forward .` line names, in order.

    Parsed out of the render rather than read from `k3s_host_dns_upstreams`: a template that
    stopped interpolating the variable leaves a correct variable and a forwarder pointed
    somewhere else, and reading the variable cannot tell those apart.
    """
    lines = _FORWARD.findall(corefile)
    assert len(lines) == 1, f"expected one `forward .` block, got {lines}"
    return lines[0].split()


def forward_problems(upstreams: list[str], corefile: str, vip: str) -> list[str]:
    """Every way the host forwarder's preference can be silently wrong."""
    problems = []
    if not upstreams:
        problems.append("no forward upstreams at all")
    elif upstreams[0] != vip:
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
    rendered = rendered_setup_text("k3s", _COREFILE)
    problems = forward_problems(forward_upstreams(rendered), rendered, _vip())
    assert not problems, (
        "daniel-box's forward order is the whole failover mechanism (see "
        f"{_COREFILE}, k3s_host_dns_upstreams). Problems: " + "; ".join(problems)
    )


def test_the_node_points_only_at_its_own_forwarder() -> None:
    """A second nameserver here makes a dead forwarder degrade silently to unfiltered DNS."""
    values = role_context(K3S_ROLE)
    assert values["k3s_node_dns_upstreams"] == ["127.0.0.1"], (
        "daniel-box resolves through its host forwarder and nothing else; a fallback entry "
        "here turns a dead forwarder from a loud outage into silent unfiltered DNS"
    )


def test_a_sequential_forward_with_fallbacks_is_clean() -> None:
    assert (
        forward_problems(
            ["10.0.0.243", "1.1.1.1"],
            "policy sequential\nmax_fails 2\n",
            "10.0.0.243",
        )
        == []
    )


def test_a_random_policy_forward_is_flagged() -> None:
    """The default policy: an ordered-looking list that is really a coin flip."""
    assert forward_problems(
        ["10.0.0.243", "1.1.1.1"], "max_fails 2\n", "10.0.0.243"
    ) == ["`policy sequential` is missing, so forward load-balances at random"]


def test_an_inverted_forward_list_is_flagged() -> None:
    assert forward_problems(
        ["1.1.1.1", "10.0.0.243"], "policy sequential\n", "10.0.0.243"
    ) == ["first upstream is '1.1.1.1', not the Pi-hole VIP"]


def test_max_fails_zero_is_flagged() -> None:
    """`max_fails 0` reads as configured and means never marked down, never health checked."""
    assert forward_problems(
        ["10.0.0.243", "1.1.1.1"],
        "policy sequential\n    max_fails 0\n",
        "10.0.0.243",
    ) == ["`max_fails 0` disables health checking entirely"]


def test_the_forward_upstream_parse_names_what_it_must_find() -> None:
    """Non-vacuity: a `forward .` line the regex stopped matching raises rather than passing.

    `forward_upstreams` asserts exactly one block, so a renamed directive fails rather than
    returning an empty list every check above would pass over.
    """
    assert forward_upstreams("    forward . 10.0.0.243 1.1.1.1 {\n") == [
        "10.0.0.243",
        "1.1.1.1",
    ]
