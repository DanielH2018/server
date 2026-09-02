#!/usr/bin/env python3
"""Guards on the slice-1 k8s manifests — the four things that fail silently.

Each of these encodes a decision from docs/archive/k3s-migration/slice-1-ingress-sso-leaf.md whose
failure mode is quiet rather than loud: nothing errors, the deploy goes green, and the
consequence shows up later as a moved VIP, a corrupted session, an ungated service, or an
unprotected edge. A rendered-YAML check cannot catch any of them (the manifests stay valid
either way) — hence a separate suite from scripts/validate/validate_k8s_manifests.py.

The four split along those consequences on 2026-09-02: the moved VIP is
`test_k8s_manifests_metallb.py`, the corrupted session and the ungated or unprotected edge
are `test_k8s_manifests_routes.py`, and the read-only RBAC that keeps Ansible the only write
path is `test_k8s_manifests_rbac.py`. What stays here is pod-level hygiene — nothing mounts
over the ServiceAccount token path, no Deployment injects service-link env — plus two guards
on the k8s play itself. The renderer and inventory vars they share are `_manifest_guards.py`.

Run: uv run pytest ansible/tests/k8s/test_k8s_manifests.py
"""

import yaml

from _helpers import ANSIBLE
from _manifest_guards import K8S, _k8s_entries, _render, _role_defaults


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


# test_routes_stay_lan_only_while_the_k8s_edge_has_no_crowdsec lived here and was REMOVED
# rather than repaired, because it had become inert in two independent ways and its green said
# nothing:
#
# 1. It detected the bouncer by SUBSTRING over raw template text. Once the CrowdSec gating
#    landed, every occurrence sat inside `{% if traefik_k8s_manage_crowdsec %}`, so both
#    strings were present whatever the flag said and the comparison read True unconditionally.
# 2. It read `k8s_public_route` from group_vars/all.yml only, so a host overriding it was never
#    evaluated — daniel-stage sets both it and the CrowdSec flag false, a consistent pair the
#    guard never looked at.
#
# The replacement is test_the_public_route_and_the_bouncer_move_together in
# test_crowdsec_optional.py: per host, on RENDERED output, with a reading proven to track the
# flag (True on daniel-box, False on daniel-stage) rather than the template text.


#
# This one exists because the failure is invisible in exactly the wrong direction. A binding
# that grants too much does not error, does not warn, and does not change any output — the
# kubeconfig keeps working, it just quietly carries more authority than the comment above it
# claims. The whole reason this identity exists instead of copying the admin kubeconfig is
# that its ceiling is enforced, so the ceiling needs a test.


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
