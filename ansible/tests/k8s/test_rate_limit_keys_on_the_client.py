"""Every rate-limit bucket must belong to one client, on both sides of Cloudflare.

The public `x.<domain>` names are Cloudflare-proxied, so a connection there comes from a
Cloudflare edge and Traefik's default rateLimit keying (the remote address) hands every client
behind that edge one shared 300/min bucket — a guessing loop throttled everyone else and the
attacker rotated edges (#1955). `ipStrategy.depth: 1` keys on the client Cloudflare appends to
X-Forwarded-For. The `.local.` names are DNS-only and reached direct, where the remote address
IS the client and no XFF arrives; Traefik's DepthStrategy returns "" for a missing header, so
depth there would pool the whole LAN into one bucket. The two hosts therefore sit on separate
Rules with separate middlewares (ansible/templates/ingressroute.yml.j2), and this guard keeps
the pairing honest however a route is next hand-rolled:

- a `-proxied` limiter carries depth >= 1 and a plain one carries no sourceCriterion;
- a Rule matching a public Host attaches a `-proxied` limiter, a Rule matching a `.local.`
  Host attaches a plain one, and no Rule mixes the two.

Both predicates are applied to the rendered tree behind a named non-vacuity set, and each has
a rejecting fixture so a predicate that stopped matching fails its own test. The livesync
file-provider routers live in a Secret's stringData rather than in an IngressRoute, so they
are parsed out of that document explicitly — they were the first routers to carry the bug.

Run: uv run pytest ansible/tests/k8s/test_rate_limit_keys_on_the_client.py
"""

import re

import pytest
from _k8s_render import rendered_docs
from lib import yaml_fast

_HOST = re.compile(r"Host\(`([^`]+)`\)")
# The file-provider form is `<namespace>-<name>@kubernetescrd`; the CRD form is the bare name.
_FILE_PROVIDER_REF = re.compile(r"^[a-z0-9-]+?-(rate-limit[a-z0-9-]*)@kubernetescrd$")

# Rendered Middlewares the census must find. Subset, not equality: a new limiter must not
# fail this, a renamed one must.
REQUIRED_LIMITERS = frozenset(
    {
        "traefik/rate-limit",
        "traefik/rate-limit-proxied",
        "traefik/rate-limit-public-livesync",
        "traefik/rate-limit-public-livesync-proxied",
        "claude-otel/rate-limit",
        "claude-otel/rate-limit-proxied",
        "longhorn-ui/rate-limit",
    }
)
# Routers matching a public Host the census must find: the macro's route, its bypass twin,
# a hand-rolled ping route, the observability-namespace route, and the file-provider pair.
REQUIRED_PUBLIC_ROUTERS = frozenset(
    {
        "karakeep",
        "karakeep-public-api-v1",
        "healthchecks-ping",
        "n8n-public-webhook",
        "grafana",
        "livesync-sync-public",
        "livesync-utils-public",
    }
)


def _limiter_problem(name: str, rate_limit: dict) -> str | None:
    """Why a rateLimit Middleware's keying does not match its name, or None."""
    depth = (
        (rate_limit.get("sourceCriterion") or {}).get("ipStrategy", {}).get("depth", 0)
    )
    if name.endswith("-proxied"):
        if depth < 1:
            return f"{name} is a -proxied limiter but keys on the remote address (depth {depth})"
    elif "sourceCriterion" in rate_limit:
        return f"{name} keys on a forwarded header, which is '' for a direct client"
    return None


def _limiter_ref(middleware: str) -> str | None:
    """The bare limiter name a middleware reference points at, or None for a non-limiter."""
    m = _FILE_PROVIDER_REF.match(middleware)
    name = m.group(1) if m else middleware
    return name if name.startswith("rate-limit") else None


def _keying_problem(match: str, middlewares: list[str]) -> str | None:
    """Why a router's Host rule and its limiter disagree, or None."""
    hosts = _HOST.findall(match)
    if not hosts:
        return None
    local = {h for h in hosts if ".local." in h}
    public = set(hosts) - local
    if local and public:
        return f"{match!r} mixes public and .local. hosts, which need different keying"
    limiters = [ref for ref in map(_limiter_ref, middlewares) if ref]
    if not limiters:
        return f"{match!r} carries no rate-limit middleware"
    for limiter in limiters:
        proxied = limiter.endswith("-proxied")
        if public and not proxied:
            return f"{match!r} is Cloudflare-proxied but {limiter} keys on the edge address"
        if local and proxied:
            return f"{match!r} is reached direct but {limiter} keys on X-Forwarded-For"
    return None


def _file_provider_routers(doc: dict):
    """(router name, rule, middlewares) for each router in a Traefik file-provider Secret."""
    for text in (doc.get("stringData") or {}).values():
        # Other Secrets carry INI, multi-document YAML and app config; only a Traefik
        # dynamic-config document declares routers, so nothing else is parsed.
        if "routers:" not in text:
            continue
        parsed = yaml_fast.safe_load(text)
        if not isinstance(parsed, dict):
            continue
        for name, router in ((parsed.get("http") or {}).get("routers") or {}).items():
            yield name, router.get("rule", ""), router.get("middlewares") or []


def test_every_rendered_limiter_keys_the_way_its_name_says():
    found: set[str] = set()
    problems: list[str] = []
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") != "Middleware" or "rateLimit" not in doc.get("spec", {}):
            continue
        name = doc["metadata"]["name"]
        found.add(f"{role}/{name}")
        if problem := _limiter_problem(name, doc["spec"]["rateLimit"]):
            problems.append(f"{role}: {problem}")
    assert not problems, "\n".join(problems)
    assert REQUIRED_LIMITERS <= found, sorted(REQUIRED_LIMITERS - found)


def test_every_rendered_router_pairs_its_host_with_the_matching_limiter():
    public_seen: set[str] = set()
    problems: list[str] = []
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") == "IngressRoute":
            routers = (
                (
                    doc["metadata"]["name"],
                    r["match"],
                    [m["name"] for m in r.get("middlewares", [])],
                )
                for r in doc["spec"]["routes"]
            )
        elif doc.get("kind") == "Secret":
            routers = _file_provider_routers(doc)
        else:
            continue
        for name, match, middlewares in routers:
            if any(".local." not in h for h in _HOST.findall(match)):
                public_seen.add(name)
            if problem := _keying_problem(match, middlewares):
                problems.append(f"{role}/{name}: {problem}")
    assert not problems, "\n".join(problems)
    assert REQUIRED_PUBLIC_ROUTERS <= public_seen, sorted(
        REQUIRED_PUBLIC_ROUTERS - public_seen
    )


@pytest.mark.parametrize(
    ("name", "rate_limit"),
    [
        ("rate-limit", {"average": 300, "burst": 150, "period": "1m"}),
        (
            "rate-limit-proxied",
            {"average": 300, "sourceCriterion": {"ipStrategy": {"depth": 1}}},
        ),
    ],
)
def test_limiter_guard_accepts_a_correctly_keyed_middleware(name, rate_limit):
    assert _limiter_problem(name, rate_limit) is None


@pytest.mark.parametrize(
    ("name", "rate_limit"),
    [
        # The pre-#1955 shape: a proxied limiter on the remote address.
        ("rate-limit-proxied", {"average": 300, "burst": 150, "period": "1m"}),
        # The issue's literal prescription: depth on the direct limiter.
        (
            "rate-limit",
            {"average": 300, "sourceCriterion": {"ipStrategy": {"depth": 1}}},
        ),
        # excludedIPs is the same '' for a missing header.
        (
            "rate-limit",
            {
                "average": 300,
                "sourceCriterion": {"ipStrategy": {"excludedIPs": ["1.1.1.1"]}},
            },
        ),
    ],
)
def test_limiter_guard_rejects_a_miskeyed_middleware(name, rate_limit):
    assert _limiter_problem(name, rate_limit) is not None


@pytest.mark.parametrize(
    ("match", "middlewares"),
    [
        ("Host(`x.example.com`)", ["rate-limit-proxied", "authelia"]),
        ("Host(`x.local.example.com`)", ["rate-limit"]),
        ("Host(`x.example.com`) && PathPrefix(`/api/`)", ["rate-limit-proxied"]),
        (
            "Host(`x.example.com`)",
            ["homelab-rate-limit-public-livesync-proxied@kubernetescrd"],
        ),
        ("Host(`x.local.example.com`)", ["homelab-rate-limit@kubernetescrd"]),
        # No Host at all is outside this guard's remit.
        ("PathPrefix(`/metrics`)", ["rate-limit"]),
    ],
)
def test_keying_guard_accepts_a_matched_pair(match, middlewares):
    assert _keying_problem(match, middlewares) is None


@pytest.mark.parametrize(
    ("match", "middlewares"),
    [
        # The pre-#1955 macro output: one Rule for both hosts, one limiter.
        ("Host(`x.example.com`) || Host(`x.local.example.com`)", ["rate-limit"]),
        ("Host(`x.example.com`)", ["rate-limit"]),
        ("Host(`x.local.example.com`)", ["rate-limit-proxied"]),
        ("Host(`x.example.com`)", ["homelab-rate-limit@kubernetescrd"]),
        ("Host(`x.example.com`)", ["authelia"]),
    ],
)
def test_keying_guard_rejects_a_mismatched_pair(match, middlewares):
    assert _keying_problem(match, middlewares) is not None
