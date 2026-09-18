"""Every public Host router requires Cloudflare's origin-pull client certificate; no `.local.` one does.

A client that reaches the origin IP directly can send a public `Host: x.<domain>` and skip
Cloudflare's WAF. The #1974 netpol admits only `cloudflare_ips` on :8443, but that list admits
any source inside Cloudflare's ranges whether or not the connection is Cloudflare's.
Authenticated Origin Pulls closes the rest: Cloudflare presents a client certificate on every
origin connection, and the `cloudflare-origin-pull` TLSOption (ansible/templates/
origin-pull.yml.j2) requires and verifies one (#1990). Traefik picks TLS options by SNI, so the
public and `.local.` hosts sit on separate IngressRoute objects and only the public one names
the option — a LAN or WireGuard client holds no certificate.

The invariant is per HOST, not per object, and the guard is shaped that way:

- a router matching a public Host names `cloudflare-origin-pull`, and a router matching a
  `.local.` Host names something else — Traefik's SNI check answers 421 when Host and SNI map
  to different options, so a `.local.` router on the origin-pull option would demand the
  certificate of every LAN client;
- every router on ONE public host names the same option. Two routers on a host with different
  options make Traefik log a warning and fall back to the default options, which require no
  certificate — the bypass documents and healthchecks' ping route share hosts with the main
  objects, which is where this would bite;
- the option a router names is rendered as a TLSOption in the router's own namespace, with the
  CA Secret it reads in that same namespace (`allowCrossNamespace` is off);
- the origin-pull option is `modern` plus clientAuth, so the public name keeps the protocol
  floor and cipher policy of the `.local.` one;
- the render FAILS when the CA file is missing, rather than shipping an empty CA that every
  handshake would fail against.

Each predicate has an accepting and a rejecting fixture, and the tree walk sits behind a named
non-vacuity set. The livesync file-provider routers live in a Secret's stringData rather than
in an IngressRoute, so they are parsed out of that document the way
test_rate_limit_keys_on_the_client.py does.

Run: uv run pytest ansible/tests/k8s/test_public_routes_require_cloudflare_origin_pull.py
"""

import re

import pytest
from _k8s_render import rendered_docs
from _manifest_guards import K8S, _render
from lib import yaml_fast

ORIGIN_PULL = "cloudflare-origin-pull"
CA_SECRET = "cloudflare-origin-pull-ca"
CA_FILE = K8S / "traefik" / "files" / "cloudflare-origin-pull-ca.crt"

_HOST = re.compile(r"Host\(`([^`]+)`\)")
_FILE_PROVIDER_REF = re.compile(
    r"^(?P<namespace>[a-z0-9-]+?)-(?P<name>[a-z0-9-]+)@kubernetescrd$"
)

# Routers matching a public Host the walk must find: the macro's public object, a bypass
# twin, the hand-rolled ping route, the observability-namespace route and the file-provider
# pair. A rename empties a glob-shaped census silently; this set names what must be there.
REQUIRED_PUBLIC_ROUTERS = frozenset(
    {
        "karakeep-public",
        "karakeep-public-api-v1",
        "healthchecks-ping-public",
        "grafana-public",
        "livesync-sync-public",
        "livesync-utils-public",
    }
)
# `.local.` routers the walk must find, so the negative half is not vacuous either.
REQUIRED_LOCAL_ROUTERS = frozenset(
    {"karakeep", "healthchecks-ping", "grafana", "livesync-probe"}
)
# Namespaces that must render both the option and its CA Secret.
REQUIRED_NAMESPACES = frozenset({"homelab", "observability"})


def _option_ref(options, namespace: str) -> tuple[str, str] | None:
    """(namespace, bare name) a router's TLS options reference resolves to, or None.

    A CRD route names the option as `{name: x}` (optionally with its own `namespace:`), and a
    file-provider router as the string `<ns>-<name>@kubernetescrd`.
    """
    if isinstance(options, dict):
        return options.get("namespace", namespace), options["name"]
    if isinstance(options, str) and (m := _FILE_PROVIDER_REF.match(options)):
        return m["namespace"], m["name"]
    return None


def _option_problem(
    match: str, options, namespace: str, rendered: set[tuple[str, str]]
) -> str | None:
    """Why a router's Host rule and its TLS options disagree, or None.

    Args:
        match: the router's rule.
        options: its `tls.options` value, CRD dict or file-provider string.
        namespace: the namespace the router itself lives in.
        rendered: every (namespace, name) the tree renders a TLSOption for.
    """
    hosts = _HOST.findall(match)
    if not hosts:
        return None
    local = {h for h in hosts if ".local." in h}
    public = set(hosts) - local
    if local and public:
        return f"{match!r} mixes public and .local. hosts, which need different TLS options"
    ref = _option_ref(options, namespace)
    if ref is None:
        return f"{match!r} names no TLS options"
    ns, name = ref
    if (ns, name) not in rendered:
        return f"{match!r} names TLS options {name}, which nothing renders in namespace {ns}"
    if public and name != ORIGIN_PULL:
        return f"{match!r} is a public host but names {name}, which requires no client certificate"
    if local and name == ORIGIN_PULL:
        return f"{match!r} is a .local. host but names {ORIGIN_PULL}, so LAN clients would need a certificate"
    return None


def _file_provider_routers(doc: dict):
    """(router name, rule, tls options) for each router in a Traefik file-provider Secret."""
    for text in (doc.get("stringData") or {}).values():
        if "routers:" not in text:
            continue
        parsed = yaml_fast.safe_load(text)
        if not isinstance(parsed, dict):
            continue
        for name, router in ((parsed.get("http") or {}).get("routers") or {}).items():
            yield name, router.get("rule", ""), (router.get("tls") or {}).get("options")


def _routers():
    """(role, router name, rule, tls options, namespace) for every rendered router."""
    for role, _tpl, doc in rendered_docs():
        namespace = doc["metadata"].get("namespace", "")
        if doc.get("kind") == "IngressRoute":
            options = (doc["spec"].get("tls") or {}).get("options")
            for r in doc["spec"]["routes"]:
                yield role, doc["metadata"]["name"], r["match"], options, namespace
        elif doc.get("kind") == "Secret":
            for name, match, options in _file_provider_routers(doc):
                yield role, name, match, options, namespace


def _rendered(kind: str) -> dict[tuple[str, str], dict]:
    return {
        (doc["metadata"].get("namespace", ""), doc["metadata"]["name"]): doc
        for _role, _tpl, doc in rendered_docs()
        if doc.get("kind") == kind
    }


def test_every_rendered_router_pairs_its_host_with_the_matching_tls_option():
    rendered = set(_rendered("TLSOption"))
    public_seen: set[str] = set()
    local_seen: set[str] = set()
    problems: list[str] = []
    for role, name, match, options, namespace in _routers():
        hosts = _HOST.findall(match)
        if any(".local." not in h for h in hosts):
            public_seen.add(name)
        elif hosts:
            local_seen.add(name)
        if problem := _option_problem(match, options, namespace, rendered):
            problems.append(f"{role}/{name}: {problem}")
    assert not problems, "\n".join(problems)
    assert REQUIRED_PUBLIC_ROUTERS <= public_seen, sorted(
        REQUIRED_PUBLIC_ROUTERS - public_seen
    )
    assert REQUIRED_LOCAL_ROUTERS <= local_seen, sorted(
        REQUIRED_LOCAL_ROUTERS - local_seen
    )


def test_every_public_host_names_one_tls_option():
    """Two routers on one host with different options fall back to Traefik's default options."""
    by_host: dict[tuple[str, str], set[str]] = {}
    for _role, _name, match, options, namespace in _routers():
        for host in _HOST.findall(match):
            if ".local." in host:
                continue
            ref = _option_ref(options, namespace)
            by_host.setdefault((namespace, host), set()).add(
                ref[1] if ref else "<none>"
            )
    assert by_host, "no public host router rendered at all"
    conflicts = {host: opts for host, opts in by_host.items() if len(opts) != 1}
    assert not conflicts, conflicts


def test_the_origin_pull_option_is_modern_plus_client_auth():
    """Same protocol floor and ciphers on the public name; only the client-cert demand differs."""
    options = _rendered("TLSOption")
    secrets = _rendered("Secret")
    seen: set[str] = set()
    for (namespace, name), doc in options.items():
        if name != ORIGIN_PULL:
            continue
        seen.add(namespace)
        modern = options[(namespace, "modern")]["spec"]
        spec = doc["spec"]
        assert spec["clientAuth"] == {
            "secretNames": [CA_SECRET],
            "clientAuthType": "RequireAndVerifyClientCert",
        }, f"{namespace}: {spec['clientAuth']}"
        assert {k: v for k, v in spec.items() if k != "clientAuth"} == modern, (
            f"{namespace}: {ORIGIN_PULL} and modern disagree on something other than clientAuth"
        )
        ca = secrets.get((namespace, CA_SECRET))
        assert ca is not None, (
            f"{namespace}: no {CA_SECRET} Secret rendered beside the option"
        )
        assert "-----BEGIN CERTIFICATE-----" in ca["stringData"]["tls.ca"], namespace
    assert REQUIRED_NAMESPACES <= seen, sorted(REQUIRED_NAMESPACES - seen)


def test_the_render_fails_without_the_ca_file(tmp_path):
    """A missing CA must stop the render, not ship a Secret with an empty CA."""
    assert CA_FILE.is_file()
    tpl = K8S / "traefik" / "templates" / "origin-pull-secret.yaml.j2"
    assert _render(tpl, k8s_namespace="homelab")  # renders against the real tree
    with pytest.raises(FileNotFoundError):
        _render(tpl, k8s_namespace="homelab", playbook_dir=str(tmp_path))


_FIXTURE_RENDERED = {
    ("homelab", "modern"),
    ("homelab", ORIGIN_PULL),
    ("observability", "modern"),
    ("observability", ORIGIN_PULL),
    ("longhorn-system", "modern"),
}


@pytest.mark.parametrize(
    ("match", "options", "namespace"),
    [
        ("Host(`x.example.com`)", {"name": ORIGIN_PULL}, "homelab"),
        (
            "Host(`x.example.com`) && PathPrefix(`/api/`)",
            {"name": ORIGIN_PULL},
            "homelab",
        ),
        ("Host(`x.local.example.com`)", {"name": "modern"}, "homelab"),
        ("Host(`x.example.com`)", f"homelab-{ORIGIN_PULL}@kubernetescrd", "homelab"),
        ("Host(`x.local.example.com`)", "homelab-modern@kubernetescrd", "homelab"),
        ("Host(`grafana.example.com`)", {"name": ORIGIN_PULL}, "observability"),
        # No Host at all is outside this guard's remit: the edge self-check is path-only.
        (
            "PathPrefix(`/.well-known/traefik-edge-selfcheck`)",
            {"name": "modern"},
            "homelab",
        ),
    ],
)
def test_option_guard_accepts_a_matched_pair(match, options, namespace):
    assert _option_problem(match, options, namespace, _FIXTURE_RENDERED) is None


@pytest.mark.parametrize(
    ("match", "options", "namespace"),
    [
        # The pre-#1990 shape: a public host on the plain option.
        ("Host(`x.example.com`)", {"name": "modern"}, "homelab"),
        # The overreach: a LAN host demanding the certificate.
        ("Host(`x.local.example.com`)", {"name": ORIGIN_PULL}, "homelab"),
        # One object carrying both hosts, which is what the macro used to render.
        (
            "Host(`x.example.com`) || Host(`x.local.example.com`)",
            {"name": ORIGIN_PULL},
            "homelab",
        ),
        ("Host(`x.example.com`)", "homelab-modern@kubernetescrd", "homelab"),
        ("Host(`x.example.com`)", None, "homelab"),
        # The right name in a namespace that renders no copy of it.
        ("Host(`x.example.com`)", {"name": ORIGIN_PULL}, "longhorn-system"),
    ],
)
def test_option_guard_rejects_a_mismatched_pair(match, options, namespace):
    assert _option_problem(match, options, namespace, _FIXTURE_RENDERED) is not None
