#!/usr/bin/env python3
"""Guards on the k8s manifests — the four things that fail silently.

Each of these encodes a decision whose failure mode is quiet rather than loud: nothing errors,
the deploy goes green, and the
consequence shows up later as a moved VIP, a corrupted session, an ungated service, or an
unprotected edge. A rendered-YAML check cannot catch any of them (the manifests stay valid
either way) — hence a separate suite from scripts/validate/k8s_manifests.py.

The four split along those consequences: the moved VIP is
`test_k8s_manifests_metallb.py`, the corrupted session and the ungated or unprotected edge
are `test_k8s_manifests_routes.py`, and the read-only RBAC that keeps Ansible the only write
path is `test_k8s_manifests_rbac.py`. What stays here is pod-level hygiene — nothing mounts
over the ServiceAccount token path — plus two guards on the k8s play itself. Pod-template
hygiene the whole fleet must satisfy is `test_pod_template_hygiene.py` (priority tier) and the
`sa-less-pods-refuse-the-default-token` and `pods-disable-service-link-env-vars` rows of
`_workload_property_rows.py`. The renderer and inventory vars shared here are
`_manifest_guards.py`.

Run: uv run pytest ansible/tests/k8s/test_k8s_manifests.py
"""

from lib import yaml_fast

from _helpers import ANSIBLE
from _k8s_render import (
    K8S_PLAY_NAME,
    deploy_play,
    pod_spec,
    rendered_build_job_text,
    rendered_docs,
)
from _manifest_guards import K8S, _k8s_entries, _render, _role_defaults


def test_the_k8s_play_does_not_filter_an_already_filtered_list():
    """deploy.yml's two plays both set_fact `containers_list`, and a set_fact persists for the
    host across plays and outranks the inventory var. So the k8s play must source from the
    snapshot taken before the Docker play narrowed it.

    Sourcing from the mutated fact fails SILENTLY: on daniel-box every entry is platform: k8s,
    so the Docker filter leaves [], the k8s filter of [] is [], and the play reports
    `ok=10 changed=0 failed=0` having deployed nothing at all.
    """
    plays = yaml_fast.safe_load((ANSIBLE / "deploy.yml").read_text())
    k8s_play = deploy_play()
    task = next(t for t in k8s_play["pre_tasks"] if "k8s-platform" in t.get("name", ""))
    expr = task["ansible.builtin.set_fact"]["containers_list"]
    assert "containers_list_unfiltered" in expr, (
        "the k8s play re-filters the Docker play's output and silently deploys nothing"
    )

    docker_play = next(p for p in plays if p.get("name") != K8S_PLAY_NAME)
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
    """A Secret volume at `/run/secrets` stops the container starting at all.

    `/run/secrets` is the Docker convention for file-mounted credentials and it does not survive
    the port. `/var/run/secrets` symlinks to `/run/secrets`, which is where Kubernetes projects
    the ServiceAccount token — a read-only Secret volume there leaves runc unable to create the
    mountpoint and the container never starts:

        mkdirat .../rootfs/run/secrets/kubernetes.io: read-only file system

    Worth a guard rather than a fix in one file: a ported compose template that uses
    /run/secrets fails as a CrashLoopBackOff whose message says nothing about the mount the
    author chose.
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
        for doc in yaml_fast.safe_load_all(rendered):
            for container in doc["spec"]["template"]["spec"]["containers"]:
                for mount in container.get("volumeMounts", []):
                    path = mount["mountPath"].rstrip("/")
                    assert not any(
                        path == r or path.startswith(r + "/") for r in reserved
                    ), (
                        f"{entry['name']} mounts {path}, shadowing the ServiceAccount token"
                    )


_TOKEN_PATHS = ("/run/secrets", "/var/run/secrets")


def _mounts(doc: dict) -> list[tuple[str, str]]:
    """(container, mount path) for every container of a rendered pod-bearing doc."""
    pod = pod_spec(doc)
    return [
        (container["name"], mount["mountPath"].rstrip("/"))
        for container in pod.get("initContainers", []) + pod.get("containers", [])
        for mount in container.get("volumeMounts", [])
    ]


def token_shadowing_mounts(doc: dict) -> list[str]:
    """Every container mount in a rendered doc at or under a ServiceAccount token path."""
    return [
        f"{name}: {path}"
        for name, path in _mounts(doc)
        if any(path == r or path.startswith(r + "/") for r in _TOKEN_PATHS)
    ]


def _every_pod_doc():
    """(where, doc) for every rendered manifest, plus the build Job no role renders alone."""
    for role, tpl, doc in rendered_docs():
        yield f"{role}/{tpl}", doc
    yield (
        "image-builder/build-job.yaml.j2",
        yaml_fast.safe_load(rendered_build_job_text()),
    )


def test_no_template_names_a_mount_under_run_secrets():
    """The rendered check above only sees deployment.yaml.j2; roles whose workloads live in
    differently-named templates (scrutiny's web.yaml.j2/influxdb.yaml.j2) would slip past it and
    CrashLoop on the same runc mountpoint error. This one parses every pod the cluster runs, of
    every kind and init containers included, as it renders, so a mount path built from a
    variable is checked at the value it takes."""
    offenders = {
        where: bad
        for where, doc in _every_pod_doc()
        if (bad := token_shadowing_mounts(doc))
    }
    assert offenders == {}, f"ServiceAccount-token-shadowing mounts: {offenders}"


def test_the_mount_census_reaches_scrutiny_and_the_build_job():
    """Non-vacuity: the two members the census exists to reach still render mounts."""
    mounted = {where.split("/")[0] for where, doc in _every_pod_doc() if _mounts(doc)}
    assert {"scrutiny", "image-builder"} <= mounted, sorted(mounted)


def test_a_mount_under_run_secrets_is_flagged():
    def cronjob(path: str) -> dict:
        init = [{"name": "init", "volumeMounts": [{"mountPath": path}]}]
        pod = {"template": {"spec": {"initContainers": init}}}
        return {"kind": "CronJob", "spec": {"jobTemplate": {"spec": pod}}}

    assert token_shadowing_mounts(cronjob("/var/run/secrets/x/")) == [
        "init: /var/run/secrets/x"
    ]
    assert token_shadowing_mounts(cronjob("/run/secretsx")) == []


# The service-link env var guard is the `pods-disable-service-link-env-vars` row of
# _workload_property_rows.py. It reads the whole rendered corpus.


# The public-route and bouncer pairing guard is test_the_public_route_and_the_bouncer_move_together
# in test_crowdsec_optional.py: per host, on RENDERED output, not on template text.


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
