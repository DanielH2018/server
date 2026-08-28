"""Every IngressRoute must keep a `tls:` block whether or not the cluster has a certificate resolver.

Prod resolves certificates through Traefik's `cloudflare` ACME DNS-01 resolver. The staging
cluster has no Cloudflare token and must never issue against the real domain's ACME account or
rate limit, so `k8s_tls_cert_resolver` is empty there and Traefik serves its own default
self-signed certificate (docs/staging-cluster.md, Decision 4).

The load-bearing property is what stays rather than what goes. An IngressRoute on the `https`
entrypoint with NO `spec.tls` is not a losing router — it is a NON-TLS router, so an HTTPS
request is never matched against it at all. That failure is invisible from the object: it
applies cleanly, `kubectl get` shows it, and Traefik logs nothing. It cost most of 2026-08-07,
and the obvious way to implement an optional resolver — wrapping the whole `tls:` block in the
conditional — reintroduces it on staging only, where nobody would be looking.

`test_k8s_manifests.py` already fails a rendered route that serves https without a tls block,
but it renders with prod's variables only. Nothing else exercises the empty-resolver branch,
which is the one where the mistake is reachable.
"""

from __future__ import annotations

import yaml
from jinja2 import Environment

from _helpers import ALL_VARS, ANSIBLE

MACRO = ANSIBLE / "templates" / "ingressroute.yml.j2"
RESOLVER_VAR = "k8s_tls_cert_resolver"

_CONTEXT = {
    "domain": "example.com",
    "k8s_namespace": "homelab",
    "k8s_public_route": True,
    "k8s_bridge_client_ip": "10.0.0.161",
}

_CALLS = {
    "ingressroute": "{{ ingressroute('freshrss', 'freshrss', 8080, false) }}",
    "monitoring_route": "{{ monitoring_route('freshrss', 'freshrss', 8080, '/api') }}",
}


def _routes(macro_call: str, resolver: str) -> list[dict]:
    source = MACRO.read_text() + "\n" + macro_call
    rendered = (
        Environment(autoescape=False)
        .from_string(source)
        .render(  # noqa: S701
            {**_CONTEXT, RESOLVER_VAR: resolver}
        )
    )
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    assert docs, (
        f"rendering {macro_call} with {RESOLVER_VAR}={resolver!r} produced no YAML documents. "
        f"The macro is broken, which is a different failure from the one this file guards."
    )
    return docs


def _tls_blocks(macro_call: str, resolver: str) -> list[dict]:
    blocks = []
    for doc in _routes(macro_call, resolver):
        assert "tls" in doc["spec"], (
            f"{doc['metadata']['name']} rendered with {RESOLVER_VAR}={resolver!r} has no "
            f"spec.tls. On the https entrypoint that makes it a NON-TLS router, which an "
            f"HTTPS request is never matched against — the route is not outranked, it is not "
            f"a candidate. It applies cleanly and Traefik logs nothing."
        )
        blocks.append(doc["spec"]["tls"])
    return blocks


def test_the_tls_key_survives_an_empty_resolver():
    """The whole point: gate the CONTENTS, never the key."""
    for call in _CALLS.values():
        for tls in _tls_blocks(call, ""):
            assert tls, (
                f"a route rendered with {RESOLVER_VAR}='' has an empty spec.tls. Traefik "
                f"needs the block to treat the router as TLS at all; `tls: {{}}` is also not "
                f"IngressRoute syntax, unlike the file provider's shorthand."
            )


def test_an_empty_resolver_names_no_resolver_and_requests_no_domains():
    for call in _CALLS.values():
        for tls in _tls_blocks(call, ""):
            assert "certResolver" not in tls, (
                f"a route rendered with {RESOLVER_VAR}='' still names "
                f"certResolver={tls.get('certResolver')!r}. Traefik would try to issue through "
                f"a resolver the cluster does not define."
            )
            assert "domains" not in tls, (
                f"a route rendered with {RESOLVER_VAR}='' still carries `domains`. That list "
                f"is the SAN set ACME is instructed to request; with no resolver it instructs "
                f"nothing and reads to a later operator as a constraint that is not enforced."
            )


def test_a_named_resolver_still_requests_the_wildcard_sans():
    """The regression that would silently stop prod renewing its wildcard certificate."""
    for call in _CALLS.values():
        for tls in _tls_blocks(call, "cloudflare"):
            assert tls.get("certResolver") == "cloudflare", (
                f"a route rendered with {RESOLVER_VAR}='cloudflare' has "
                f"certResolver={tls.get('certResolver')!r}."
            )
            sans = [san for d in tls.get("domains", []) for san in d.get("sans", [])]
            assert "*.example.com" in sans and "*.local.example.com" in sans, (
                f"a route rendered with a resolver requests SANs {sans}. Both wildcards must "
                f"stay: omitting them makes Traefik request a certificate per literal Host "
                f"rule, which is a separate issuance each and different rate-limit arithmetic."
            )


def test_the_tls_options_are_named_on_both_branches():
    """`options` is not part of the resolver; losing it silently drops the modern TLS profile."""
    for resolver in ("cloudflare", ""):
        for tls in _tls_blocks(_CALLS["ingressroute"], resolver):
            assert tls.get("options", {}).get("name") == "modern", (
                f"a route rendered with {RESOLVER_VAR}={resolver!r} has "
                f"options={tls.get('options')!r}. The profile applies regardless of how the "
                f"certificate was obtained, so it belongs outside the resolver's conditional."
            )


def test_production_still_declares_a_resolver():
    """An empty default would disable prod's certificate issuance everywhere, silently."""
    value = yaml.safe_load(ALL_VARS.read_text())[RESOLVER_VAR]
    assert value == "cloudflare", (
        f"{RESOLVER_VAR} in group_vars/all.yml is {value!r}. Production issues through the "
        f"`cloudflare` ACME resolver defined in the traefik role's static config; emptying the "
        f"DEFAULT rather than overriding it per host would drop every cluster to self-signed "
        f"certificates, and browsers are the only thing that would report it."
    )
