"""Pi-hole answers for the cluster nodes' own hostnames.

Nothing else on the LAN does. A node reaches its peer through a client-side alias in an SSH
client config, and that file is deliberately absent from all three homelab hosts, so before
these records `daniel-box` resolved nowhere from daniel-server. The visible cost was
otel-sweep-watch reporting `box: unreachable (Could not resolve hostname daniel-box)` on
every run it made there — a daily false finding on a check that only works if it is believed.

The bare name is the half that matters. otel-sweep addresses its machines from a fixed enum
holding `daniel-box` and `daniel-server`, deliberately not caller-configurable, so a
`.lan`-only record would leave that tool exactly as broken as no record at all.

Two properties, checked against two different artifacts, because neither one alone can see
both. The rendered ConfigMap carries the NAMES but not the addresses — the render harness
stubs every `hostvars` lookup — so the addresses are pinned against the template source
instead, where the thing worth asserting is that they are derived rather than typed in.
"""

from __future__ import annotations

import re

from _helpers import REPO
from _k8s_render import rendered_texts

# The census this file asserts over, named rather than derived. A test that globbed for
# `host-record=` lines and asserted over whatever it found would pass on an empty set the
# moment the directive were renamed, which is a failure a count cannot see.
REQUIRED = ("daniel-box", "daniel-server")

_TEMPLATE = REPO / "ansible/roles/k8s/pihole/templates/config/pihole-dnsmasq.conf.j2"


def _dnsmasq_conf() -> str:
    """The rendered dnsmasq config, as it is embedded in Pi-hole's ConfigMap.

    Read as TEXT, not as a parsed doc: dnsmasq directives are lines inside a string value,
    so a YAML round-trip would confirm the key exists and say nothing about its content.
    """
    texts = [
        text
        for role, tpl, text in rendered_texts()
        if role == "pihole" and tpl.endswith("configmap.yaml.j2")
    ]
    assert texts, "pihole's configmap template rendered nothing — the census is empty"
    return "\n".join(texts)


def _host_record_names(conf: str) -> set[str]:
    """Every name a `host-record=` line answers for.

    One directive may carry several names before the address, which is exactly how a bare
    name and its `.lan` alias share a line. The address is dropped: it renders as a stub
    here, and the template test below is what pins it.
    """
    names: set[str] = set()
    for line in conf.splitlines():
        line = line.strip()
        if not line.startswith("host-record="):
            continue
        for field in line[len("host-record=") :].split(",")[:-1]:
            if field:
                names.add(field)
    return names


def test_both_cluster_nodes_answer_under_their_bare_hostname():
    names = _host_record_names(_dnsmasq_conf())
    for host in REQUIRED:
        assert host in names, (
            f"{host} has no host-record, so it resolves nowhere on the LAN and every tool "
            "addressing it by name fails on name resolution rather than on reachability"
        )


def test_the_lan_alias_is_present_too_but_is_not_what_the_sweep_uses():
    # The rejecting half of the pair above, and the reason both halves exist: a record
    # written only as `daniel-box.lan` satisfies the naming convention daniel-pi sets while
    # leaving otel-sweep's fixed enum resolving nothing. Asserting the bare name alone would
    # not catch a later tidy-up that moved these under `.lan` only, and asserting the alias
    # alone would not catch the bug this fixes.
    names = _host_record_names(_dnsmasq_conf())
    for host in REQUIRED:
        assert f"{host}.lan" in names, (
            f"{host}.lan should follow the daniel-pi convention"
        )


def test_the_addresses_are_derived_from_inventory_not_typed_in():
    # A literal here would answer correctly today and keep answering after a node moved,
    # which is worse than not answering at all — a stale A record sends every caller to
    # whatever now holds that address.
    source = _TEMPLATE.read_text()
    for host in REQUIRED:
        pattern = rf"host-record=[^\n]*\bhostvars\['{re.escape(host)}'\]\.server_ip"
        assert re.search(pattern, source), (
            f"{host}'s host-record must take its address from hostvars, not a literal"
        )


def test_the_census_reads_real_directives():
    # Non-vacuity. Every assertion above is a membership test against `_host_record_names`,
    # and an empty set would fail them loudly — but only because REQUIRED is non-empty. This
    # pins the parser itself against a record that predates this change and that nothing
    # here touches, so a parser that quietly stopped matching cannot read as a clean tree.
    assert "daniel-pi.lan" in _host_record_names(_dnsmasq_conf()), (
        "the parser stopped reading host-record lines it used to read"
    )
