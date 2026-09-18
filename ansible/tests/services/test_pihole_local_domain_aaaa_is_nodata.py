"""Pi-hole answers an AAAA for a `.local.<domain>` name with NODATA, never with `::`.

Since dnsmasq 2.86 an `address=` line carrying only an IPv4 sends every other record type
upstream, so the `.local.` wildcard needs a second directive to keep AAAA lookups local.
Until #2010 that directive was `address=/local.<domain>/::`, which answers `::` — the IPv6
NULL address — to every client that asks. A client preferring IPv6 tries `::` first:
Happy Eyeballs falls back after a stall, grpc-go retried `[::]:443` forever (2026-08-10).
`local=/local.<domain>/` keeps the query local and answers it NODATA, the form dnsmasq's
manual names for restoring the pre-2.86 behaviour.

Both halves are asserted against the rendered ConfigMap, because the property is a pair:
the `local=` line present AND the `::` line gone. Either alone reads as fixed while the
other still answers.
"""

import re

from _k8s_render import rendered_texts


def _dnsmasq_conf() -> str:
    texts = [
        text
        for role, tpl, text in rendered_texts()
        if role == "pihole" and tpl.endswith("configmap.yaml.j2")
    ]
    assert texts, "pihole's configmap template rendered nothing — the census is empty"
    return "\n".join(texts)


def _directives(conf: str) -> list[str]:
    return [
        line.strip() for line in conf.splitlines() if not line.lstrip().startswith("#")
    ]


def test_the_local_wildcard_is_declared_local_so_aaaa_stays_nodata():
    lines = _directives(_dnsmasq_conf())
    assert any(re.fullmatch(r"local=/local\.[^/]+/", line) for line in lines), (
        "no `local=/local.<domain>/` — an AAAA for a .local. name is forwarded upstream"
    )


def test_no_address_line_answers_the_ipv6_null_address():
    # The rejecting half: the line #2010 removed, and the shape a well-meant "keep AAAA
    # local" revert would put back.
    lines = _directives(_dnsmasq_conf())
    offenders = [line for line in lines if re.fullmatch(r"address=/[^/]+/(::|#)", line)]
    assert not offenders, (
        f"{offenders} answer the NULL address; a client preferring IPv6 dials `::` first"
    )


def test_the_a_wildcard_is_still_answered():
    # Non-vacuity for the two above: a template that dropped the wildcard block entirely
    # would satisfy both — no `::` line, and `local=` could survive alone as an NXDOMAIN
    # for the whole domain. The A record is the thing the block exists to serve.
    lines = _directives(_dnsmasq_conf())
    assert any(
        re.fullmatch(r"address=/local\.[^/]+/\d+\.\d+\.\d+\.\d+", line)
        for line in lines
    ), "the .local. A wildcard is gone — every .local. name now resolves nowhere"
