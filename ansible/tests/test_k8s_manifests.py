#!/usr/bin/env python3
"""Guards on the slice-1 k8s manifests — the four things that fail silently.

Each of these encodes a decision from docs/archive/k3s-migration/slice-1-ingress-sso-leaf.md whose
failure mode is quiet rather than loud: nothing errors, the deploy goes green, and the
consequence shows up later as a moved VIP, a corrupted session, an ungated service, or an
unprotected edge. A rendered-YAML check cannot catch any of them (the manifests stay valid
either way) — hence a separate suite from scripts/validate_k8s_manifests.py.

Run: uv run pytest ansible/tests/test_k8s_manifests.py
"""

import re
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

from validate_k8s_manifests import ansible_bool
from _helpers import ANSIBLE


K3S = ANSIBLE / "roles" / "setup" / "k3s"
K8S = ANSIBLE / "roles" / "k8s"
ALL_VARS = yaml.safe_load(
    (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
)
BOX_VARS = yaml.safe_load(
    (ANSIBLE / "inventory" / "host_vars" / "daniel-box.yml").read_text()
)


def _render(path: Path, **ctx) -> str:
    """Render a template with the given context; undefined values are left to raise."""
    env = Environment(
        loader=FileSystemLoader([str(path.parent), str(ANSIBLE / "templates")]),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.globals.update(ctx)
    return env.get_template(path.name).render(**ctx)


def _k8s_entries() -> list[dict]:
    return [c for c in BOX_VARS["containers_list"] if c.get("platform") == "k8s"]


def _role_defaults(role: str) -> dict:
    """A role's defaults with `{{ ... }}` inside VALUES expanded, as Ansible expands them.

    n8n's image defaults are `"{{ k8s_registry_pull_host }}/n8n:latest"`, and that var is
    itself `"localhost:{{ k8s_registry_port }}"` — so the raw YAML carries braces two levels
    deep. Passed through unexpanded they reach the rendered manifest, where `{` opens a flow
    mapping and the whole document fails to parse for a reason that has nothing to do with the
    template being tested.
    """
    values = {
        **ALL_VARS,
        **yaml.safe_load((K8S / role / "defaults" / "main.yml").read_text()),
    }
    env = Environment(loader=FileSystemLoader([str(ANSIBLE / "templates")]))
    # `bool` is an Ansible filter, not a Jinja builtin — a group_var using it (k8s_no_mutate)
    # would fail this loop with "No filter named 'bool'". Same shim scripts/ registers.
    env.filters["bool"] = ansible_bool
    for _ in range(5):
        pending = {k: v for k, v in values.items() if isinstance(v, str) and "{{" in v}
        if not pending:
            break
        for key, value in pending.items():
            values[key] = env.from_string(value).render(values)
    return values


def _ip_to_int(addr: str) -> int:
    parts = [int(p) for p in addr.split(".")]
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


def _pool_docs() -> list[dict]:
    """IPAddressPool documents in FILE order — the order kubectl applies them in."""
    rendered = _render(
        K3S / "templates" / "metallb-pool.yaml.j2",
        k3s_metallb_ingress_vip=ALL_VARS["k3s_metallb_ingress_vip"],
        k3s_metallb_pool=yaml.safe_load((K3S / "defaults" / "main.yml").read_text())[
            "k3s_metallb_pool"
        ],
    )
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    return [d for d in docs if d["kind"] == "IPAddressPool"]


def _pools() -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in _pool_docs()}


def test_ingress_pool_is_a_single_address_that_is_never_auto_assigned():
    """autoAssign: false is the whole reservation. Without it MetalLB hands the ingress
    address to whichever LoadBalancer Service asks first, and ingress moves — after that
    address is in DNS and, from slice 6, in the router's port-forward."""
    ingress = _pools()["ingress-pool"]
    assert ingress["spec"]["autoAssign"] is False
    assert ingress["spec"]["addresses"] == [f"{ALL_VARS['k3s_metallb_ingress_vip']}/32"]


def test_general_pool_does_not_contain_the_ingress_vip():
    """A /32 reservation means nothing if the auto-assigning pool still covers the address."""
    start, end = _pools()["homelab-pool"]["spec"]["addresses"][0].split("-")
    vip = _ip_to_int(ALL_VARS["k3s_metallb_ingress_vip"])
    assert not (_ip_to_int(start) <= vip <= _ip_to_int(end))


def test_the_general_pool_narrows_before_the_ingress_pool_is_created():
    """kubectl applies documents in file order and MetalLB's validating webhook rejects
    overlapping pools, so the wide pool has to narrow first. Applying ingress-pool ahead of it
    failed on daniel-box (2026-08-02) with:

        CIDR "10.0.0.240/32" in pool "ingress-pool" overlaps with already
        defined CIDR "10.0.0.240/29"

    — homelab-pool still covered .240-.250 at validation time. Reordering the file is the whole
    fix, which is exactly why it is worth a guard: nothing about the YAML looks order-sensitive.
    """
    names = [d["metadata"]["name"] for d in _pool_docs()]
    assert names.index("homelab-pool") < names.index("ingress-pool")


def test_the_k8s_play_does_not_filter_an_already_filtered_list():
    """deploy.yml's two plays both set_fact `containers_list`, and a set_fact persists for the
    host across plays and outranks the inventory var. So the k8s play must source from the
    snapshot taken before the Docker play narrowed it.

    Sourcing from the mutated fact fails SILENTLY: on daniel-box every entry is platform: k8s,
    so the Docker filter leaves [], the k8s filter of [] is [], and the play reports
    `ok=10 changed=0 failed=0` having deployed nothing at all (daniel-box, 2026-08-02).
    """
    plays = yaml.safe_load((ANSIBLE / "deploy.yml").read_text())
    k8s_play = next(p for p in plays if "k8s" in p["name"].lower())
    task = next(t for t in k8s_play["pre_tasks"] if "k8s-platform" in t.get("name", ""))
    expr = task["ansible.builtin.set_fact"]["containers_list"]
    assert "containers_list_unfiltered" in expr, (
        "the k8s play re-filters the Docker play's output and silently deploys nothing"
    )

    docker_play = next(p for p in plays if p is not k8s_play)
    names = [t.get("name", "") for t in docker_play["pre_tasks"]]
    assert names.index(
        "Preserve the unfiltered container list for the k8s play"
    ) < names.index("Restrict this play to Docker-platform containers"), (
        "the snapshot must be taken before the list is narrowed"
    )


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
    """The slice-1 hazard from design.md: two Authelias writing one SQLite file corrupt it.
    The database must live under the mount backed by the PVC, never on a path shared with
    daniel-server's bind mount."""
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


# Inventory-shaped stubs for deployment templates that reach outside their role defaults
# (uptime-kuma's hostAliases render the -k8s hostname list from daniel-box's inventory).
_DEPLOYMENT_STUBS = {
    # domain and the ingress VIP already arrive via _role_defaults (group_vars).
    "hostvars": {
        "daniel-box": {
            "containers_list": [
                {
                    "name": "stub",
                    "hostname": "stub-k8s",
                    "extra_hostnames": ["stub2-k8s"],
                },
            ]
        }
    },
}


def test_nothing_mounts_over_the_serviceaccount_token_path():
    """`/run/secrets` is the Docker convention for file-mounted credentials and it does not
    survive the port. `/var/run/secrets` symlinks to `/run/secrets`, which is where Kubernetes
    projects the ServiceAccount token — a read-only Secret volume there leaves runc unable to
    create the mountpoint and the container never starts (daniel-box, 2026-08-02):

        mkdirat .../rootfs/run/secrets/kubernetes.io: read-only file system

    Worth a guard rather than a fix in one file: slice 2 ports ~33 more services from compose
    templates that all use /run/secrets, and the symptom is a CrashLoopBackOff whose message
    says nothing about the mount the author chose.
    """
    reserved = ("/run/secrets", "/var/run/secrets")
    for entry in _k8s_entries():
        tpl = K8S / entry["name"] / "templates" / "deployment.yaml.j2"
        if not tpl.exists():
            continue
        rendered = _render(
            tpl,
            container_item=entry,
            **_DEPLOYMENT_STUBS,
            **_role_defaults(entry["name"]),
        )
        for doc in yaml.safe_load_all(rendered):
            for container in doc["spec"]["template"]["spec"]["containers"]:
                for mount in container.get("volumeMounts", []):
                    path = mount["mountPath"].rstrip("/")
                    assert not any(
                        path == r or path.startswith(r + "/") for r in reserved
                    ), (
                        f"{entry['name']} mounts {path}, shadowing the ServiceAccount token"
                    )


def test_no_template_names_a_mount_under_run_secrets():
    """The rendered check above only sees deployment.yaml.j2; roles whose workloads live in
    differently-named templates (scrutiny's web.yaml.j2/influxdb.yaml.j2) slipped past it and
    CrashLooped on the same runc mountpoint error (2026-08-10, second occurrence). A textual
    scan over EVERY k8s template needs no render context and catches the whole class."""
    offenders = []
    for tpl in K8S.glob("*/templates/*.j2"):
        for line in tpl.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("mountPath:"):
                path = stripped.split(":", 1)[1].strip().strip("\"'").rstrip("/")
                if (
                    path == "/run/secrets"
                    or path.startswith("/run/secrets/")
                    or path == "/var/run/secrets"
                    or path.startswith("/var/run/secrets/")
                ):
                    offenders.append(f"{tpl}: {stripped}")
    assert not offenders, f"ServiceAccount-token-shadowing mounts: {offenders}"


def test_every_deployment_disables_service_link_env_vars():
    """Kubernetes injects legacy Docker-link env vars for every Service in the namespace —
    <NAME>_SERVICE_HOST, <NAME>_PORT_<n>_TCP and so on. Any app that reads its own config from
    <NAME>_* env vars then picks them up as configuration. Authelia did, and exited before
    serving anything (daniel-box, 2026-08-02):

        error occurred performing deprecation mapping for keys 'server.host', 'server.port',
        and 'server.path' to new key server.address: the new key already exists with value
        'tcp4://:9091' but the deprecated keys and the new key can't both be configured

    Triggering it needs only that a Service name match an app's env-var prefix, which is the
    normal case in this namespace — so the guard covers every workload, not just Authelia.
    """
    for entry in _k8s_entries():
        tpl = K8S / entry["name"] / "templates" / "deployment.yaml.j2"
        if not tpl.exists():
            continue
        rendered = _render(
            tpl,
            container_item=entry,
            **_DEPLOYMENT_STUBS,
            **_role_defaults(entry["name"]),
        )
        for doc in yaml.safe_load_all(rendered):
            assert doc["spec"]["template"]["spec"].get("enableServiceLinks") is False, (
                f"{entry['name']} inherits Docker-link env vars for every Service in the namespace"
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
    """The check that has to scale: slice 2 hand-authors ~33 more IngressRoutes, and a
    missing middleware is an ungated service that returns 200 and looks fine.

    Iterates every document, not just the first: a role may ship more than one IngressRoute
    (healthchecks ships its ping bypass alongside the UI route), and reading only the first
    would leave the extra ones unchecked — exactly where an ungated route would hide.
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


def test_routes_stay_lan_only_while_the_k8s_edge_has_no_crowdsec():
    """k8s_public_route and the CrowdSec bouncer have to move together. A public Host rule
    while the k8s Traefik carries no bouncer is an unprotected edge one DNS record away.

    The bouncer is detected where it actually lives since B1: the Middleware CRD in
    dynamic.yaml.j2 (crowdsecLapiKeyFile — the key moved out of the static config so the
    read-only kubeconfig can't read it) AND its entrypoint-wide attachment in the static
    config. Both, because a declared-but-unattached middleware protects nothing."""
    static = (K8S / "traefik" / "templates" / "static-config.yaml.j2").read_text()
    dynamic = (K8S / "traefik" / "templates" / "dynamic.yaml.j2").read_text()
    has_bouncer = (
        "crowdsecLapiKeyFile" in dynamic and "-crowdsec@kubernetescrd" in static
    )
    assert ALL_VARS["k8s_public_route"] == has_bouncer, (
        "k8s_public_route is only safe once the k8s Traefik runs the CrowdSec bouncer "
        "(slice 6) — flip both or neither"
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


#
# This one exists because the failure is invisible in exactly the wrong direction. A binding
# that grants too much does not error, does not warn, and does not change any output — the
# kubeconfig keeps working, it just quietly carries more authority than the comment above it
# claims. The whole reason this identity exists instead of copying the admin kubeconfig is
# that its ceiling is enforced, so the ceiling needs a test.

K3S_DEFAULTS = yaml.safe_load((K3S / "defaults" / "main.yml").read_text())
READ_VERBS = {"get", "list", "watch"}


def _readonly_rbac_docs() -> list[dict]:
    rendered = _render(
        K3S / "templates" / "readonly-rbac.yaml.j2",
        sys_user=ALL_VARS["sys_user"],
        k3s_readonly_sa_name=K3S_DEFAULTS["k3s_readonly_sa_name"],
        k3s_readonly_sa_namespace=K3S_DEFAULTS["k3s_readonly_sa_namespace"],
        k3s_readonly_crd_api_groups=K3S_DEFAULTS["k3s_readonly_crd_api_groups"],
    )
    return [d for d in yaml.safe_load_all(rendered) if d]


def _readonly_rules() -> list[dict]:
    return [
        rule
        for doc in _readonly_rbac_docs()
        if doc["kind"] == "ClusterRole"
        for rule in doc["rules"]
    ]


# Resources that turn cluster read access into cluster compromise. `secrets` is every
# credential the cluster holds — the built-in `view` role excludes it deliberately and the
# additive role must not put it back. `pods/exec` and its siblings are arbitrary code
# execution inside a running workload, which RBAC models as a subresource `create` but which
# reads, in a list of get/list/watch, like just more access.
FORBIDDEN_RESOURCES = {"secrets", "pods/exec", "pods/attach", "pods/portforward"}


def _grant_violations(rules: list[dict]) -> list[str]:
    """Every way a rule list exceeds read-only. Empty means the ceiling holds."""
    problems = []
    for rule in rules:
        groups = set(rule.get("apiGroups", []))
        named = set(rule.get("resources", []))
        extra = set(rule.get("verbs", [])) - READ_VERBS
        if extra:
            problems.append(f"verbs {sorted(extra)} on {sorted(named)}")
        if named & FORBIDDEN_RESOURCES:
            problems.append(f"resource {sorted(named & FORBIDDEN_RESOURCES)}")
        # A bare wildcard over the core group sweeps secrets back in without naming them.
        if "*" in named and ("" in groups or "*" in groups):
            problems.append(f"wildcard resources over apiGroups {sorted(groups)}")
    return problems


def test_readonly_role_stays_read_only():
    assert _grant_violations(_readonly_rules()) == []


def test_the_read_only_check_rejects_a_widened_role():
    """The guard above only means something if it fails on a role that oversteps. These are
    the three shapes a widening actually takes — a write verb, a named secret read, and a
    wildcard that never says "secrets" out loud."""
    for rule in (
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "delete"]},
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["*"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["get"]},
    ):
        assert _grant_violations([rule]), f"widening not caught: {rule}"


def test_readonly_bindings_never_reference_a_writing_clusterrole():
    """The additive ClusterRole is audited by the tests above; a roleRef pointing somewhere
    else routes around all of them. Only `view` and this role's own name are permitted."""
    allowed = {"view", K3S_DEFAULTS["k3s_readonly_sa_name"]}
    bindings = [d for d in _readonly_rbac_docs() if d["kind"] == "ClusterRoleBinding"]
    assert bindings, "no ClusterRoleBinding rendered"
    for binding in bindings:
        name = binding["roleRef"]["name"]
        assert name in allowed, f"{binding['metadata']['name']} binds to '{name}'"


def _headlamp_rbac_docs() -> list[dict]:
    rendered = _render(
        K8S / "headlamp" / "templates" / "rbac.yaml.j2",
        **_role_defaults("headlamp"),
    )
    return [d for d in yaml.safe_load_all(rendered) if d]


def test_headlamp_cluster_identity_stays_read_only():
    """Headlamp runs with `-unsafe-use-service-account-token`, so it never asks the browser
    for a credential — every request that gets past Authelia acts as this ServiceAccount, and
    the ClusterIP Service is reachable from any pod besides. The SA's ceiling is therefore the
    dashboard's security boundary, not a defence-in-depth layer, and it gets the same guard as
    the shell's homelab-readonly identity above."""
    rules = [
        rule
        for doc in _headlamp_rbac_docs()
        if doc["kind"] == "ClusterRole"
        for rule in doc["rules"]
    ]
    assert rules, "no ClusterRole rendered"
    assert _grant_violations(rules) == []


def test_headlamp_binds_only_to_read_only_cluster_roles():
    """A roleRef pointing anywhere else routes around the rule audit above. Upstream's Helm
    chart binds `cluster-admin` — copying a fragment of it back in is the realistic mistake."""
    allowed = {"view", "headlamp-cluster-read"}
    bindings = [d for d in _headlamp_rbac_docs() if d["kind"] == "ClusterRoleBinding"]
    assert bindings, "no ClusterRoleBinding rendered"
    for binding in bindings:
        name = binding["roleRef"]["name"]
        assert name in allowed, f"{binding['metadata']['name']} binds to '{name}'"


def test_headlamp_keeps_its_serviceaccount_token_mounted():
    """The flag that removes the token prompt reads the projected SA token. Setting
    automountServiceAccountToken false — or omitting serviceAccountName, which silently falls
    back to the namespace `default` SA with no permissions — leaves a dashboard that loads,
    authenticates nobody, and shows an empty cluster."""
    doc = yaml.safe_load(
        _render(
            K8S / "headlamp" / "templates" / "deployment.yaml.j2",
            container_item=next(c for c in _k8s_entries() if c["name"] == "headlamp"),
            **_role_defaults("headlamp"),
        )
    )
    spec = doc["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "headlamp"
    assert spec["automountServiceAccountToken"] is True
    args = spec["containers"][0]["args"]
    assert "-unsafe-use-service-account-token" in args


def test_homepage_kubernetes_widget_wiring_holds_together():
    """Three pieces have to agree or the widget renders EMPTY rather than erroring: the config
    must ask for cluster mode, the pod must name the SA that mode authenticates with, and that
    SA must be able to read the metrics API. Any one of them missing looks identical from the
    dashboard — a tile with no numbers, which reads as "nothing to report"."""
    role = K8S / "homepage"
    assert "mode: cluster" in (role / "templates" / "kubernetes.yaml.j2").read_text()

    deployment = yaml.safe_load(
        _render(
            role / "templates" / "deployment.yaml.j2",
            container_item=next(c for c in _k8s_entries() if c["name"] == "homepage"),
            **_role_defaults("homepage"),
        )
    )
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "homepage"

    rbac = [
        d
        for d in yaml.safe_load_all(
            _render(role / "templates" / "rbac.yaml.j2", **_role_defaults("homepage"))
        )
        if d
    ]
    rules = [rule for d in rbac if d["kind"] == "ClusterRole" for rule in d["rules"]]
    assert _grant_violations(rules) == []
    assert any("metrics.k8s.io" in rule.get("apiGroups", []) for rule in rules), (
        "no metrics.k8s.io read: every CPU/memory figure in the widget would be blank"
    )


def test_readonly_role_covers_the_crd_groups_this_homelab_deploys():
    """`view` covers no CRDs and nothing aggregates into it, so a group missing from the
    list degrades silently: the kubeconfig still works, that one `kubectl get` says
    Forbidden, and the caller falls back to sudo — which is the thing this replaced."""
    groups = set(K3S_DEFAULTS["k3s_readonly_crd_api_groups"])
    route = (ANSIBLE / "templates" / "ingressroute.yml.j2").read_text()
    assert "traefik.io" in route, (
        "ingressroute macro no longer uses the traefik.io group"
    )
    assert "traefik.io" in groups, "IngressRoute/Middleware unreadable without sudo"


def test_no_task_reads_a_dotted_secret_key_by_jsonpath():
    """`kubectl get secret -o jsonpath={.data.users_database\\.yml}` does not error on a key
    whose name contains a dot — it prints nothing and exits 0.

    Authelia's read-back guard used that form, so it saw "no existing hash" on every run,
    regenerated the argon2 hash with a fresh salt, rewrote the Secret and rolled the pod.
    Nothing in the deploy output said so; it just never converged. Fetch {.data} and index
    the map in Ansible instead, where a missing key is visible.
    """
    for tasks in sorted((ANSIBLE / "roles" / "k8s").glob("*/tasks/main.yml")):
        for line in tasks.read_text().splitlines():
            body = line.split("#", 1)[0]
            if "jsonpath={.data." in body and "\\." in body:
                raise AssertionError(
                    f"{tasks.relative_to(ANSIBLE)}: jsonpath cannot address a dotted key — "
                    f"it returns empty and exits 0. Fetch {{.data}} and index it. Line: {line.strip()}"
                )


def test_metallb_service_annotations_use_the_universe_tf_namespace():
    """metallb.io is the API GROUP of the CRDs; Service annotations keep metallb.universe.tf.

    Kubernetes accepts any annotation key and MetalLB ignores unrecognised ones, so the wrong
    prefix is completely silent — the Service is created, an address is assigned from the
    auto-assign pool instead of the reserved VIP, and the deploy is green. Traefik ran on
    10.0.0.241 instead of 10.0.0.240 through an entire slice-1 bring-up because of this.
    """
    for tpl in sorted((K8S).glob("*/templates/service.yaml.j2")):
        for i, line in enumerate(tpl.read_text().splitlines(), 1):
            body = line.split("#", 1)[0]
            if "metallb.io/" in body:
                raise AssertionError(
                    f"{tpl.relative_to(ANSIBLE)}:{i} uses a metallb.io/ Service annotation, "
                    f"which MetalLB silently ignores — use metallb.universe.tf/. Line: {line.strip()}"
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
    """`default` is reserved: Traefik registers a TLSOption of that name as the global default
    options, never under <namespace>-default@kubernetescrd. An IngressRoute naming it
    explicitly therefore fails to build, while the object sits there looking perfectly valid:

        error "unknown TLS options: homelab-default@kubernetescrd"

    Every router carrying that reference stops serving. Guard both halves — the name is not
    the reserved one, and the name the macro asks for is one the traefik role defines.
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
