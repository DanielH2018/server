"""Every forwardAuth runs behind a chain that clears the request-target headers first.

Traefik's forwardAuth, with `trustForwardHeader: true`, copies an incoming X-Forwarded-Host,
X-Forwarded-Uri and X-Forwarded-Method onto the auth request and fills them from the request
only when they are absent. Authelia authorizes the object those three headers name. So the
three must be rebuilt from the request itself, and the headers Middleware that deletes them
has to run before forwardAuth on every route. Each namespace with protected routes therefore
carries a unit of three Middlewares (roles/k8s/authelia/templates/forwardauth-middleware.yaml.j2
has the reasoning):

- `authelia-strip-forwarded-target`, whose customRequestHeaders clears exactly those three.
  It must never clear X-Forwarded-For (an empty value breaks forwardAuth's append, and Authelia
  keys regulation on it) or X-Forwarded-Proto (set earlier by `default-headers`);
- `authelia-forwardauth`, which no route may name directly;
- `authelia`, a chain of the two in that order, which is what every route names.

The guard walks the rendered tree: every IngressRoute route, and the file-provider routers the
livesync gate Secret carries. The predicates are pure functions over a list of docs, so each
rule has a rejecting fixture beside the corpus check, and two named sets keep the corpus check
from passing on an empty walk.

Run: uv run pytest ansible/tests/k8s/test_forwardauth_rebuilds_request_target.py
"""

import pytest
from _k8s_render import rendered_docs
from lib import yaml_fast

STRIP = {"X-Forwarded-Host": "", "X-Forwarded-Uri": "", "X-Forwarded-Method": ""}

# Namespaces whose `authelia` chain the census must find guarded. Subset, not equality: a
# fourth namespace must not fail this, a dropped one must.
REQUIRED_NAMESPACES = frozenset({"homelab", "longhorn-system", "observability"})
# Routers the walker must resolve to a guarded forwardAuth: a macro route and its public
# twin, the hand-rolled Traefik dashboard, the longhorn and observability routes, and both
# livesync file-provider routers.
REQUIRED_ROUTERS = frozenset(
    {
        "homelab/homepage",
        "homelab/homepage-public",
        "homelab/traefik-dashboard",
        "longhorn-system/longhorn-frontend",
        "observability/grafana",
        "observability/grafana-public",
        "file/livesync-utils-public",
        "file/livesync-utils-local",
    }
)


def _middlewares(docs: list[dict]) -> dict[tuple[str, str], dict]:
    """(namespace, name) -> spec for every Middleware in docs."""
    return {
        (d["metadata"]["namespace"], d["metadata"]["name"]): d.get("spec") or {}
        for d in docs
        if d.get("kind") == "Middleware"
    }


def _file_ref(ref: str, index: dict) -> tuple[str, str]:
    """Resolve a file-provider `<namespace>-<name>@kubernetescrd` reference.

    Both halves may contain dashes, so each split is tried against what is rendered. An
    unresolvable reference keeps its first split, which the caller reports as missing.
    """
    body = ref.removesuffix("@kubernetescrd")
    splits = [(body[:i], body[i + 1 :]) for i, c in enumerate(body) if c == "-"]
    return next((s for s in splits if s in index), splits[0] if splits else ("", body))


def _routers(docs: list[dict], index: dict):
    """(router id, [(namespace, name), ...]) for every router in docs."""
    for d in docs:
        ns = d.get("metadata", {}).get("namespace", "")
        if d.get("kind") == "IngressRoute":
            for route in d["spec"].get("routes") or []:
                refs = [
                    (m.get("namespace", ns), m["name"])
                    for m in route.get("middlewares") or []
                ]
                yield f"{ns}/{d['metadata']['name']}", refs
        elif d.get("kind") == "Secret":
            for text in (d.get("stringData") or {}).values():
                # Only a Traefik dynamic-config document declares routers; other Secrets carry
                # INI and app config that is not worth parsing.
                if "routers:" not in text:
                    continue
                parsed = yaml_fast.safe_load(text)
                if not isinstance(parsed, dict):
                    continue
                routers = (parsed.get("http") or {}).get("routers") or {}
                for name, router in routers.items():
                    refs = [
                        _file_ref(m, index)
                        for m in router.get("middlewares") or []
                        if m.endswith("@kubernetescrd")
                    ]
                    yield f"file/{name}", refs


def _chain_problem(ns: str, name: str, members: list[str], index: dict) -> str | None:
    """Why a chain that contains a forwardAuth does not clear the target headers first."""
    specs = [index.get((ns, m)) for m in members]
    for member, spec in zip(members, specs, strict=True):
        if spec is None:
            return f"{ns}/{name} names {member}, which nothing renders in {ns}"
    if not any("forwardAuth" in s for s in specs):
        return None
    first = specs[0].get("headers", {}).get("customRequestHeaders")
    if first != STRIP:
        return (
            f"{ns}/{name} runs a forwardAuth, but its first member {members[0]} clears "
            f"{sorted(first or {})} rather than exactly {sorted(STRIP)}"
        )
    return None


def _analyse(docs: list[dict]) -> tuple[list[str], set[str], set[str]]:
    """(problems, namespaces with a guarded chain, routers reaching a guarded forwardAuth)."""
    index = _middlewares(docs)
    problems: list[str] = []
    guarded: set[tuple[str, str]] = set()
    for (ns, name), spec in index.items():
        if "chain" not in spec:
            continue
        members = [m["name"] for m in spec["chain"].get("middlewares") or []]
        if problem := _chain_problem(ns, name, members, index):
            problems.append(problem)
        elif any("forwardAuth" in index[(ns, m)] for m in members):
            guarded.add((ns, name))
    routers: set[str] = set()
    for router, refs in _routers(docs, index):
        for ref in refs:
            if "forwardAuth" in index.get(ref, {}):
                problems.append(f"{router} names forwardAuth {ref[1]} directly")
            elif ref in guarded:
                routers.add(router)
    return problems, {ns for ns, _ in guarded}, routers


def test_every_rendered_forwardauth_sits_behind_the_strip():
    problems, namespaces, routers = _analyse([doc for _r, _t, doc in rendered_docs()])
    assert not problems, "\n".join(problems)
    assert REQUIRED_NAMESPACES <= namespaces, sorted(REQUIRED_NAMESPACES - namespaces)
    assert REQUIRED_ROUTERS <= routers, sorted(REQUIRED_ROUTERS - routers)


# --- Fixtures: one clean unit, then one mutation per rule. ---------------------------------


def _mw(name: str, spec: dict, ns: str = "ns") -> dict:
    return {
        "kind": "Middleware",
        "metadata": {"name": name, "namespace": ns},
        "spec": spec,
    }


def _unit(strip: dict | None = None, members: list[str] | None = None) -> list[dict]:
    return [
        _mw(
            "strip",
            {
                "headers": {
                    "customRequestHeaders": dict(STRIP if strip is None else strip)
                }
            },
        ),
        _mw(
            "fa",
            {"forwardAuth": {"address": "http://authelia", "trustForwardHeader": True}},
        ),
        _mw(
            "authelia",
            {
                "chain": {
                    "middlewares": [{"name": m} for m in members or ["strip", "fa"]]
                }
            },
        ),
    ]


def _route(*middlewares: str) -> dict:
    return {
        "kind": "IngressRoute",
        "metadata": {"name": "app", "namespace": "ns"},
        "spec": {"routes": [{"middlewares": [{"name": m} for m in middlewares]}]},
    }


def _file_route(*middlewares: str) -> dict:
    refs = "".join(f"\n        - {m}" for m in middlewares)
    text = f"http:\n  routers:\n    gate:\n      middlewares:{refs}\n"
    return {
        "kind": "Secret",
        "metadata": {"name": "gate", "namespace": "ns"},
        "stringData": {"d.yaml": text},
    }


def test_route_through_the_chain_is_clean():
    problems, namespaces, routers = _analyse(
        [*_unit(), _route("authelia"), _file_route("ns-authelia@kubernetescrd")]
    )
    assert problems == []
    assert namespaces == {"ns"}
    assert routers == {"ns/app", "file/gate"}


@pytest.mark.parametrize(
    "route", [_route("fa"), _file_route("ns-fa@kubernetescrd")], ids=["crd", "file"]
)
def test_route_naming_the_forwardauth_directly_is_flagged(route):
    problems, _, _ = _analyse([*_unit(), route])
    assert any("directly" in p for p in problems), problems


def test_strip_missing_method_is_flagged():
    strip = {k: v for k, v in STRIP.items() if k != "X-Forwarded-Method"}
    problems, namespaces, _ = _analyse(_unit(strip=strip))
    assert problems and namespaces == set()


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Forwarded-Proto"])
def test_strip_clearing_client_or_proto_is_flagged(header):
    problems, _, _ = _analyse(_unit(strip={**STRIP, header: ""}))
    assert problems


def test_strip_after_the_forwardauth_is_flagged():
    problems, _, _ = _analyse(_unit(members=["fa", "strip"]))
    assert problems


def test_chain_member_missing_from_its_namespace_is_flagged():
    docs = _unit(members=["strip", "fa", "absent"])
    problems, _, _ = _analyse(docs)
    assert any("nothing renders" in p for p in problems), problems
