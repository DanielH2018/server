"""The edge: Authelia's session config, and what every IngressRoute must carry.

An authed route without the forward-auth and rate-limit middlewares is an open service; an
`https` route without `tls:` never matches at all; the CrowdSec LAPI route must never gain a
public host rule; a public host rule must be reachable only from the Docker edge; and every
route's TLSOption must exist and not be the one named `default`. None of these fails a render.
"""

import re

import yaml
from _manifest_guards import ALL_VARS, K8S, _k8s_entries, _render


def _k8s_authelia_config() -> dict:
    entry = next(c for c in _k8s_entries() if c["name"] == "authelia")
    defaults = yaml.safe_load((K8S / "authelia" / "defaults" / "main.yml").read_text())
    rendered = _render(
        K8S / "authelia" / "templates" / "config-secret.yaml.j2",
        container_item=entry,
        domain="example.com",
        email="stub@example.com",
        authelia_jwt="stub",
        authelia_secret="stub",
        authelia_storage="stub",
        authelia_user="stub",
        authelia_password_hash="stub",
        authelia_oidc_hmac_secret="stub",
        authelia_oidc_rsa_key_content="STUBKEY",
        authelia_client_password_hash="stub",
        **ALL_VARS,
        **defaults,
    )
    doc = yaml.safe_load(rendered)
    return yaml.safe_load(doc["stringData"]["configuration.yml"])


def test_k8s_session_cookie_name_is_set():
    """The Docker Authelia this once guarded against retired at E7 (2026-08-13, archived to
    roles/containers/archive/authelia) — the k8s portal is the only one now, so the
    cookie-collision risk this test used to check is moot. What's left worth pinning: the
    session cookie name actually comes from the intended var, and every cookie variant
    (the `.local.` LAN one and the public one) is named off it."""
    k8s_name = yaml.safe_load((K8S / "authelia" / "defaults" / "main.yml").read_text())[
        "authelia_k8s_cookie_name"
    ]

    session = _k8s_authelia_config()["session"]
    assert session["name"] == k8s_name
    assert all(c["name"].startswith(k8s_name) for c in session["cookies"])


def test_k8s_authelia_database_is_on_its_own_volume():
    """The slice-1 hazard from design.md:

    two Authelias writing one SQLite file corrupt it. The database must live under the mount backed
    by the PVC, never on a path shared with daniel-server's bind mount.
    """
    db_path = _k8s_authelia_config()["storage"]["local"]["path"]
    assert db_path.startswith("/config/")

    deployment = yaml.safe_load(
        _render(
            K8S / "authelia" / "templates" / "deployment.yaml.j2",
            container_item=next(c for c in _k8s_entries() if c["name"] == "authelia"),
            **ALL_VARS,
            **yaml.safe_load((K8S / "authelia" / "defaults" / "main.yml").read_text()),
        )
    )
    spec = deployment["spec"]["template"]["spec"]
    mount = next(
        m
        for m in spec["containers"][0]["volumeMounts"]
        if db_path.startswith(m["mountPath"] + "/")
    )
    volume = next(v for v in spec["volumes"] if v["name"] == mount["name"])
    assert "persistentVolumeClaim" in volume, (
        f"{db_path} is backed by {volume} — an emptyDir loses every TOTP enrolment on restart"
    )


# IngressRoutes that deliberately skip forward-auth on a service whose `use_authelia` is true.
# Keyed by the IngressRoute's metadata.name so adding another unauthenticated route to an
# otherwise-gated service has to be a conscious edit here, with a reason, rather than something
# that slips in behind a passing test.
AUTHELIA_BYPASS_ROUTES = {
    "healthchecks-ping": (
        "Monitored jobs POST to /ping/<uuid> with no credentials. Gating it would not fail "
        "loudly — every check would silently go red while the jobs kept working. Carried over "
        "from the Docker role's hand-rolled healthchecks-ping router."
    ),
    # ("n8n-monitoring" retired 2026-08-16: monitor-bridge moved in-cluster on 2026-08-14 and
    # a 30-day Traefik access-log census found no other caller, so the route was deleted
    # rather than narrowed. It was the only route reaching a read-write API with neither
    # Authelia nor a ClientIP matcher.)
    # The three below are the B5-prep NATIVE public bypasses on the UNSUFFIXED names —
    # emitted by the ingressroute() macro from each entry's bridge_bypass_prefixes once
    # k8s_public_route flipped. Each reproduces a hole the Docker edge already serves for the
    # same session-less callers; post-B5 those callers arrive at this edge directly.
    # ("healthchecks-public-ping" retired 2026-08-15 with the -k8s suffix: once
    # `healthchecks-ping` covered the public name too, this was a strict subset of it.)
    "n8n-public-webhook": (
        "External services POST /webhook/ with no session; gating it silently breaks every "
        "registered webhook while the callers keep reporting success."
    ),
    "karakeep-public-api": (
        "The karakeep API bypass the Docker edge already serves publicly (trailing slash "
        "keeps /api-docs and siblings out); the app's own API keys are the gate."
    ),
}


def test_every_authed_service_carries_forward_auth_and_rate_limit():
    """The check that has to scale:

    slice 2 hand-authors ~33 more IngressRoutes, and a missing middleware is an ungated service that
    returns 200 and looks fine.

    Iterates every document, not just the first: a role may ship more than one IngressRoute
    (healthchecks ships its ping bypass alongside the UI route), and reading only the first would
    leave the extra ones unchecked — exactly where an ungated route would hide.
    """
    for entry in _k8s_entries():
        route_tpl = K8S / entry["name"] / "templates" / "ingressroute.yaml.j2"
        if not route_tpl.exists():
            continue
        rendered = _render(
            route_tpl,
            container_item=entry,
            domain="example.com",
            **ALL_VARS,
        )
        for doc in (d for d in yaml.safe_load_all(rendered) if d):
            name = doc["metadata"]["name"]
            for route in doc["spec"]["routes"]:
                middlewares = [m["name"] for m in route["middlewares"]]
                # rate-limit is unconditional — it is the brute-force protection, and the
                # login portal (use_authelia: false) is the route that needs it most. It
                # applies to bypass routes too: skipping auth never means skipping this.
                assert any(m.startswith("rate-limit") for m in middlewares), name
                # A strangler-bridge route carries no forward-auth on purpose — the Docker
                # edge it is reachable from has already applied Authelia. Exempting it by
                # metadata.name would exempt the service's normal route too, since both live
                # in one document, so the exemption is the ClientIP clause itself. That makes
                # the two conditions inseparable: drop the guard and this test starts
                # demanding the auth back.
                if "ClientIP(" in route["match"]:
                    continue
                if entry.get("use_authelia") and name not in AUTHELIA_BYPASS_ROUTES:
                    assert "authelia" in middlewares, name


def test_every_https_route_carries_tls():
    """An IngressRoute on the `https` entrypoint with no `spec.tls` is a NON-TLS router, and
    the k8s Traefik sets no entrypoint-level TLS — so TLS is decided per router and an HTTPS
    request is only ever matched against TLS routers.

    The route still applies cleanly, `kubectl get` still shows it, and Traefik logs nothing.
    Requests just fall through to whatever other router matches the host. That is how the three
    `-monitoring` routes were dead from B4c until 2026-08-07 while looking correct, and it cost
    an investigation that chased priority and ClientIP instead. Globs `ingressroute*` so a
    route in a second template is covered too — the monitoring routes live in their own file.
    """
    for entry in _k8s_entries():
        for route_tpl in sorted(
            (K8S / entry["name"] / "templates").glob("ingressroute*.j2")
        ):
            rendered = _render(
                route_tpl,
                container_item=entry,
                domain="example.com",
                **ALL_VARS,
            )
            for doc in (d for d in yaml.safe_load_all(rendered) if d):
                if "https" not in doc["spec"].get("entryPoints", []):
                    continue
                assert doc["spec"].get("tls"), (
                    f"{doc['metadata']['name']} ({route_tpl.parent.parent.name}) serves the "
                    "https entrypoint without a tls block — it will never match a request"
                )


def test_the_lapi_route_never_gains_a_public_host_rule():
    """The CrowdSec LAPI is machine-key-gated ban management — k8s_public_route flipping
    true must not drag it onto the public internet. Its route passes public=false to the
    ingressroute() macro; this pins the rendered result so a macro refactor can't undo it."""
    entry = next(c for c in _k8s_entries() if c["name"] == "crowdsec")
    route_tpl = K8S / "crowdsec" / "templates" / "ingressroute.yaml.j2"
    rendered = _render(
        route_tpl, container_item=entry, domain="example.com", **ALL_VARS
    )
    for doc in (d for d in yaml.safe_load_all(rendered) if d):
        if doc["metadata"]["name"] != "crowdsec":
            continue
        for route in doc["spec"]["routes"]:
            hosts = re.findall(r"Host\(`([^`]+)`\)", route["match"])
            public = [h for h in hosts if not h.endswith(".local.example.com")]
            assert not public, (
                f"the LAPI IngressRoute matches {public} on the public domain — the "
                "ban-management API must stay LAN-only whatever k8s_public_route says"
            )
        break
    else:
        raise AssertionError(
            "the LAPI IngressRoute (metadata.name crowdsec) not found in the rendered output"
        )


def test_every_public_host_rule_is_reachable_only_from_the_docker_edge():
    """The companion to the test above, for the hole the strangler bridge opens in it.

    That test guards a variable; this one guards the rendered rules, which is where the
    exposure actually lives. A bridged route matches the real public hostname while
    k8s_public_route is still false, so the variable check stays green either way — and a
    bridged route that lost its ClientIP clause would be an un-Authelia'd service answering
    the public name to anything on the LAN that can spell `curl --resolve`.
    """
    domain = "example.com"
    for entry in _k8s_entries():
        route_tpl = K8S / entry["name"] / "templates" / "ingressroute.yaml.j2"
        if not route_tpl.exists():
            continue
        rendered = _render(route_tpl, container_item=entry, domain=domain, **ALL_VARS)
        for doc in (d for d in yaml.safe_load_all(rendered) if d):
            for route in doc["spec"]["routes"]:
                match = route["match"]
                hosts = re.findall(r"Host\(`([^`]+)`\)", match)
                public = [h for h in hosts if not h.endswith(f".local.{domain}")]
                if not public or ALL_VARS["k8s_public_route"]:
                    continue
                assert "ClientIP(" in match, (
                    f"{doc['metadata']['name']} matches {public} on the public domain with "
                    "no ClientIP guard, so it is reachable from the whole LAN without "
                    "passing the Docker edge's Authelia and CrowdSec"
                )


def _tlsoption_names() -> set:
    rendered = _render(
        K8S / "traefik" / "templates" / "dynamic.yaml.j2",
        **ALL_VARS,
        **yaml.safe_load((K8S / "traefik" / "defaults" / "main.yml").read_text()),
    )
    return {
        d["metadata"]["name"]
        for d in yaml.safe_load_all(rendered)
        if d and d.get("kind") == "TLSOption"
    }


def test_routes_reference_a_tlsoption_that_exists_and_is_not_named_default():
    """`default` is reserved:

    Traefik registers a TLSOption of that name as the global default options, never under
    <namespace>-default@kubernetescrd. An IngressRoute naming it explicitly therefore fails to
    build, while the object sits there looking perfectly valid:

        error "unknown TLS options: homelab-default@kubernetescrd"

    Every router carrying that reference stops serving. Guard both halves — the name is not the
    reserved one, and the name the macro asks for is one the traefik role defines.
    """
    defined = _tlsoption_names()
    assert defined, "the traefik role no longer defines any TLSOption"
    assert "default" not in defined, (
        "a TLSOption named 'default' is registered as Traefik's global default and cannot be "
        "referenced by name from an IngressRoute"
    )
    for entry in _k8s_entries():
        tpl = K8S / entry["name"] / "templates" / "ingressroute.yaml.j2"
        if not tpl.exists():
            continue
        rendered = _render(tpl, container_item=entry, **ALL_VARS)
        # Every document: a role may ship more than one IngressRoute, and a second one naming
        # a TLSOption that does not exist fails exactly as loudly as the first would.
        for doc in (d for d in yaml.safe_load_all(rendered) if d):
            named = doc["spec"]["tls"]["options"]["name"]
            assert named in defined, (
                f"{doc['metadata']['name']} references TLS options '{named}', which the "
                f"traefik role does not define (it defines {sorted(defined)})"
            )


# monitoring_route() call sites allowed to serve PathPrefix(`/`). Empty on purpose — see the
# test below. Adding a name here needs the reason written beside it, the same shape as
# AUTHELIA_BYPASS_ROUTES above.
_WIDE_MONITORING_ROUTES: dict[str, str] = {}


def test_no_monitoring_route_serves_a_bare_path_prefix():
    """A `-monitoring` route matching PathPrefix(`/`) exposes the whole backend API.

    The macro says guard 2 is "only the endpoint the check reads" (ansible/templates/
    ingressroute.yml.j2), and until 2026-08-24 two of eight call sites ignored it:
    loki-homelab and ical-proxy, both also widened to the entire pod CIDR. On an
    `auth_enabled: false` Loki that combination gave every pod read of every log line, a push
    path to forge entries, and the delete handler — verified live, not inferred.

    The ClientIP guard cannot be relied on to compensate. Everything arriving through Traefik
    carries Traefik's identity, so a NetworkPolicy cannot tell callers apart, and the CIDR is
    the only discriminator the route has.

    A comment asking the next author to remember is what failed here, so this is a test.
    """
    offenders = []
    for entry in _k8s_entries():
        for route_tpl in sorted(
            (K8S / entry["name"] / "templates").glob("ingressroute*.j2")
        ):
            rendered = _render(
                route_tpl,
                container_item=entry,
                domain="example.com",
                **ALL_VARS,
            )
            for doc in (d for d in yaml.safe_load_all(rendered) if d):
                name = doc["metadata"]["name"]
                if not name.endswith("-monitoring"):
                    continue
                for route in doc["spec"].get("routes", []):
                    if "PathPrefix(`/`)" not in route.get("match", ""):
                        continue
                    if name in _WIDE_MONITORING_ROUTES:
                        continue
                    offenders.append(f"{name} ({entry['name']})")
    assert not offenders, (
        "monitoring route(s) serving PathPrefix(`/`), which exposes the whole backend API: "
        f"{sorted(offenders)}. Narrow the prefix to the endpoint the caller actually reads, or "
        "add the route to _WIDE_MONITORING_ROUTES with the reason."
    )
