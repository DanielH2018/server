#!/usr/bin/env python3
"""Guards on the slice-1 k8s manifests — the four things that fail silently.

Each of these encodes a decision from docs/k3s-migration/slice-1-ingress-sso-leaf.md whose
failure mode is quiet rather than loud: nothing errors, the deploy goes green, and the
consequence shows up later as a moved VIP, a corrupted session, an ungated service, or an
unprotected edge. A rendered-YAML check cannot catch any of them (the manifests stay valid
either way) — hence a separate suite from scripts/validate_k8s_manifests.py.

Run: uv run pytest ansible/tests/test_k8s_manifests.py
"""

from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

ANSIBLE = Path(__file__).resolve().parents[1]
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


def _ip_to_int(addr: str) -> int:
    parts = [int(p) for p in addr.split(".")]
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


# --- 1. the ingress VIP is reserved, structurally -----------------------------------------


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


# --- 2. the two Authelias never share state ------------------------------------------------


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
        **ALL_VARS,
        **defaults,
    )
    doc = yaml.safe_load(rendered)
    return yaml.safe_load(doc["stringData"]["configuration.yml"])


def test_k8s_session_cookie_name_differs_from_the_docker_one():
    """Both portals serve the cookie domain local.<domain>, and a browser holds one cookie
    per (domain, name) pair. Sharing the name means logging in at one portal overwrites the
    other's cookie — and with in-memory sessions the overwritten side bounces the user to
    login. Symptom: signing into the k8s portal signs you out of everything on daniel-server."""
    docker_config = (
        ANSIBLE
        / "roles"
        / "containers"
        / "authelia"
        / "templates"
        / "configuration.yml.j2"
    ).read_text()
    k8s_name = yaml.safe_load((K8S / "authelia" / "defaults" / "main.yml").read_text())[
        "authelia_k8s_cookie_name"
    ]

    assert k8s_name not in docker_config
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
        doc = yaml.safe_load(
            _render(
                tpl,
                container_item=entry,
                **ALL_VARS,
                **yaml.safe_load(
                    (K8S / entry["name"] / "defaults" / "main.yml").read_text()
                ),
            )
        )
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for mount in container.get("volumeMounts", []):
                path = mount["mountPath"].rstrip("/")
                assert not any(
                    path == r or path.startswith(r + "/") for r in reserved
                ), f"{entry['name']} mounts {path}, shadowing the ServiceAccount token"


# --- 3. protected services are actually protected -------------------------------------------


def test_every_authed_service_carries_forward_auth_and_rate_limit():
    """The check that has to scale: slice 2 hand-authors ~33 more IngressRoutes, and a
    missing middleware is an ungated service that returns 200 and looks fine."""
    for entry in _k8s_entries():
        route_tpl = K8S / entry["name"] / "templates" / "ingressroute.yaml.j2"
        if not route_tpl.exists():
            continue
        doc = yaml.safe_load(
            _render(
                route_tpl,
                container_item=entry,
                domain="example.com",
                **ALL_VARS,
            )
        )
        middlewares = [m["name"] for m in doc["spec"]["routes"][0]["middlewares"]]
        # rate-limit is unconditional — it is the brute-force protection, and the login
        # portal (use_authelia: false) is the route that needs it most.
        assert "rate-limit" in middlewares, entry["name"]
        if entry.get("use_authelia"):
            assert "authelia" in middlewares, entry["name"]


def test_routes_stay_lan_only_while_the_k8s_edge_has_no_crowdsec():
    """k8s_public_route and the CrowdSec bouncer have to move together. A public Host rule
    while the k8s Traefik carries no bouncer is an unprotected edge one DNS record away."""
    static = (K8S / "traefik" / "templates" / "static-config.yaml.j2").read_text()
    has_bouncer = "crowdsecLapiKey" in static
    assert ALL_VARS["k8s_public_route"] == has_bouncer, (
        "k8s_public_route is only safe once the k8s Traefik runs the CrowdSec bouncer "
        "(slice 6) — flip both or neither"
    )
