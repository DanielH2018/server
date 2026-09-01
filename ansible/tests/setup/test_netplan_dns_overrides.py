"""Guard: the netplan DNS template must suppress DHCPv4, DHCPv6 AND router-advertisement DNS.

WHY THIS EXISTS. Kubernetes caps a pod's resolv.conf at 3 nameservers. kubelet copies
`/run/systemd/resolve/resolv.conf` into every pod with an effective dnsPolicy of Default, so
while the DHCP lease contributes resolvers that file runs six deep and kubelet emits a
`DNSConfigForming` warning on every sync for this node's hostNetwork pods (metallb speaker,
node-exporter).

TWO SEPARATE KNOBS, AND THE SECOND IS THE ONE THAT BITES. `dhcp6-overrides` does NOT govern
IPv6 resolvers supplied by router advertisement — those need `ra-overrides`, which maps to
networkd's `[IPv6AcceptRA] UseDNS=false`. The first deploy set only the two DHCP overrides,
took the file from 6 nameservers to 4, and kubelet went on warning with
`1.1.1.1 1.0.0.1 2001:558:feed::1` still present.

WHY IT NEEDS A GUARD RATHER THAN THE COMMENT IT ALREADY HAS. Dropping `ra-overrides` breaks
nothing visibly: DNS still resolves, every service stays up, and the only signal is a kubelet
warning nobody reads. A regression here is silent by construction, which is the same shape as
the other checks in this suite.

The three keys are asserted per LINK, not per file, so adding a second interface to
`k3s_node_dns_dhcp_links` cannot half-configure one of them.
"""

from __future__ import annotations

import re

from _helpers import ROLES as _ROLES

_TEMPLATE = _ROLES / "setup/k3s/templates/netplan-dns.yaml.j2"

# Every override block that can feed a resolver into systemd-resolved. `ra-overrides` is the
# one an author is most likely to leave out, because the other two are the obvious pair.
_REQUIRED_OVERRIDES = ("dhcp4-overrides", "dhcp6-overrides", "ra-overrides")

_COMMENT = re.compile(r"^\s*#")


def overrides_missing_use_dns_false(text: str) -> list[str]:
    """Override keys that are absent, or present without `use-dns: false` under them.

    Reads structurally rather than by substring: an override key mentioned in a COMMENT (this
    template explains all three at length) must not satisfy the requirement, or the guard would
    pass on a file whose prose survived and whose config did not.
    """
    lines = [ln for ln in text.splitlines() if not _COMMENT.match(ln)]
    missing = []
    for key in _REQUIRED_OVERRIDES:
        satisfied = False
        for i, line in enumerate(lines):
            if line.strip() != f"{key}:":
                continue
            indent = len(line) - len(line.lstrip())
            for later in lines[i + 1 :]:
                if later.strip() and (len(later) - len(later.lstrip())) <= indent:
                    break
                if later.strip() == "use-dns: false":
                    satisfied = True
                    break
            if satisfied:
                break
        if not satisfied:
            missing.append(key)
    return missing


def test_the_live_template_suppresses_all_three_resolver_sources() -> None:
    missing = overrides_missing_use_dns_false(_TEMPLATE.read_text())
    assert not missing, (
        f"{_TEMPLATE.name} must set `use-dns: false` under every override that can feed a "
        "resolver, or the node's resolv.conf goes back over the 3-nameserver pod cap and "
        "kubelet warns DNSConfigForming on every sync. Missing: " + ", ".join(missing)
    )


def test_all_three_present_is_clean() -> None:
    doc = """
        eth0:
          dhcp4-overrides:
            use-dns: false
          dhcp6-overrides:
            use-dns: false
          ra-overrides:
            use-dns: false
    """
    assert overrides_missing_use_dns_false(doc) == []


def test_a_missing_ra_override_is_flagged() -> None:
    """The exact regression: the two DHCP overrides look complete and leave RA untouched."""
    doc = """
        eth0:
          dhcp4-overrides:
            use-dns: false
          dhcp6-overrides:
            use-dns: false
    """
    assert overrides_missing_use_dns_false(doc) == ["ra-overrides"]


def test_an_override_present_but_not_disabling_dns_is_flagged() -> None:
    """Presence is not the requirement — `use-dns: false` under it is."""
    doc = """
        eth0:
          dhcp4-overrides:
            use-dns: false
          dhcp6-overrides:
            use-dns: false
          ra-overrides:
            use-domains: false
    """
    assert overrides_missing_use_dns_false(doc) == ["ra-overrides"]


def test_a_commented_out_override_does_not_satisfy_the_rule() -> None:
    """This template explains all three at length; prose must not stand in for config."""
    doc = """
        eth0:
          dhcp4-overrides:
            use-dns: false
          dhcp6-overrides:
            use-dns: false
          # ra-overrides:
          #   use-dns: false
    """
    assert overrides_missing_use_dns_false(doc) == ["ra-overrides"]


def test_use_dns_false_under_a_SIBLING_key_does_not_satisfy_it() -> None:
    """The scan must stop at the end of the block, or one setting would clear them all."""
    doc = """
        eth0:
          ra-overrides:
            use-domains: false
          dhcp4-overrides:
            use-dns: false
          dhcp6-overrides:
            use-dns: false
    """
    assert overrides_missing_use_dns_false(doc) == ["ra-overrides"]
