"""Rendered-manifest property rows whose subject is a workload, a Service or every object.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

from validate.validate_lib.k8s_schema import NO_SCHEMA, schema_error

from _k8s_render import pod_spec
from _property_table import Property

POD_KINDS = frozenset({"Deployment", "DaemonSet", "StatefulSet", "CronJob", "Job"})

OBSERVABILITY = "observability"


OBSERVABILITY_MACRO_SERVICES = frozenset(
    {"grafana", "kube-state-metrics", "otel-collector"}
)


def _macro_services(role: str, tpl: str, doc: dict):
    if doc.get("kind") != "Service":
        return
    name = doc["metadata"]["name"]
    if (role == OBSERVABILITY and name in OBSERVABILITY_MACRO_SERVICES) or role in {
        "nut",
        "karakeep",
    }:
        yield f"{role}/{name}", doc


def _service_namespace_offence(role: str, doc: dict) -> str | None:
    expected = OBSERVABILITY if role == OBSERVABILITY else "homelab"
    actual = doc["metadata"].get("namespace")
    if actual == expected:
        return None
    return f"Service renders into {actual!r}, not {expected!r}"


# k8s_registry_pull_host is `localhost:<port>`. Nothing else on the plane pulls from a loopback
# registry, so the prefix is the whole test of "built here".
_BUILT_PREFIX = "localhost:"


def _built_image_containers(role: str, tpl: str, doc: dict):
    if doc.get("kind") not in POD_KINDS:
        return
    spec = pod_spec(doc)
    for container in (spec.get("initContainers") or []) + (
        spec.get("containers") or []
    ):
        if str(container.get("image", "")).startswith(_BUILT_PREFIX):
            yield f"{doc['metadata']['name']}:{container['name']}", container


def _pull_always_offence(role: str, container: dict) -> str | None:
    if container.get("imagePullPolicy") == "Always":
        return None
    return (
        f"imagePullPolicy is {container.get('imagePullPolicy')!r}: the pod runs whatever the "
        "node cached, and the drift gate fails the deploy afterwards. Add "
        "`imagePullPolicy: Always` next to `image:`"
    )


def _every_object(role: str, tpl: str, doc: dict):
    yield f"{doc.get('apiVersion')} {doc.get('kind')}", doc


def _no_schema_offence(role: str, doc: dict) -> str | None:
    if schema_error(doc) is not NO_SCHEMA:
        return None
    return (
        "no vendored schema. Add the group's file to CORE_GROUP_FILES, or the CRD to "
        "VENDORED, in scripts/validate/refresh_vendored_schemas.py, and run it"
    )


KARAKEEP_CHROME = "karakeep-chrome"

# The paths chromium writes under a read-only root. Adding one is a tightening; removing one
# needs evidence from the pod, not from the manifest.
CHROME_WRITABLE_PATHS = frozenset({"/tmp", "/var/cache/fontconfig"})


def _karakeep_chrome(role: str, tpl: str, doc: dict):
    if (
        role == "karakeep"
        and doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == KARAKEEP_CHROME
    ):
        yield KARAKEEP_CHROME, doc


def _chrome_writable_offence(role: str, doc: dict) -> str | None:
    spec = pod_spec(doc)
    chrome = next(
        (c for c in spec.get("containers", []) if c["name"] == "chrome"), None
    )
    if chrome is None:
        return "no container named chrome"
    if chrome.get("securityContext", {}).get("readOnlyRootFilesystem") is not True:
        return "the root is not read-only, so this property no longer means anything"
    # Only an emptyDir counts: a PVC at the path satisfies a mount check while being RWO and
    # node-pinning a pod that has no state to keep.
    empty_dirs = {v["name"] for v in spec.get("volumes", []) if "emptyDir" in v}
    writable = {
        m["mountPath"]
        for m in chrome.get("volumeMounts", [])
        if m["name"] in empty_dirs
    }
    if missing := sorted(CHROME_WRITABLE_PATHS - writable):
        return f"chrome cannot write {missing}: no emptyDir is mounted there"
    return None


def _chrome_deployment(fontconfig_volume: dict) -> dict:
    return {
        "spec": {
            "template": {
                "spec": {
                    "volumes": [
                        {"name": "chrome-tmp", "emptyDir": {}},
                        fontconfig_volume,
                    ],
                    "containers": [
                        {
                            "name": "chrome",
                            "securityContext": {"readOnlyRootFilesystem": True},
                            "volumeMounts": [
                                {"name": "chrome-tmp", "mountPath": "/tmp"},
                                {
                                    "name": "chrome-fontconfig-cache",
                                    "mountPath": "/var/cache/fontconfig",
                                },
                            ],
                        }
                    ],
                }
            }
        }
    }


HOMEPAGE_REVALIDATE_PATH = "/api/revalidate"


def _homepage_deployment(role: str, tpl: str, doc: dict):
    if (
        role == "homepage"
        and doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "homepage"
    ):
        yield "homepage", doc


def _startup_revalidation_offence(role: str, doc: dict) -> str | None:
    """Why the `homepage` container does not re-render `/` at startup, or None.

    The postStart hook must call /api/revalidate and must never exit non-zero: a failing
    postStart kills the container, which turns a cosmetic defect into a crashloop.
    """
    container = next(
        (c for c in pod_spec(doc).get("containers", []) if c.get("name") == "homepage"),
        None,
    )
    if container is None:
        return "no container named homepage"
    command = (
        container.get("lifecycle", {})
        .get("postStart", {})
        .get("exec", {})
        .get("command")
    )
    if not command:
        return "no lifecycle.postStart hook on the homepage container"
    text = " ".join(command)
    if HOMEPAGE_REVALIDATE_PATH not in text:
        return f"postStart hook does not call {HOMEPAGE_REVALIDATE_PATH}"
    if "exit 0" not in text:
        return "postStart hook can exit non-zero, which would kill the container"
    return None


def _homepage_with_post_start(command: list[str]) -> dict:
    container = {
        "name": "homepage",
        "lifecycle": {"postStart": {"exec": {"command": command}}},
    }
    return {"spec": {"template": {"spec": {"containers": [container]}}}}


def _pod_specs(role: str, tpl: str, doc: dict):
    # The object name is in the key because one template can carry two Deployments (pihole's
    # does), and a key naming only the file would merge them.
    if doc.get("kind") in POD_KINDS:
        yield f"{doc['kind']}/{doc['metadata']['name']}", pod_spec(doc)


def _automount_offence(role: str, pod: dict) -> str | None:
    if pod.get("serviceAccountName"):
        return None
    if pod.get("automountServiceAccountToken") is False:
        return None
    return (
        "names no serviceAccountName and does not set automountServiceAccountToken: false, "
        "so the default SA's token is mounted into a pod that never uses it"
    )


def _service_links_offence(role: str, pod: dict) -> str | None:
    if pod.get("enableServiceLinks") is False:
        return None
    return "inherits Docker-link env vars for every Service in the namespace"


# 88 pod templates render, Jobs and CronJobs included. Close enough to notice a contraction.
_MIN_POD_TEMPLATES = 78
# One pod per role the census was written against, so a lost role is named, not counted.
_POD_TEMPLATES_MUST_FIND = frozenset(
    {
        "Deployment/prometheus",
        "Deployment/valheim",
        "Deployment/valheim-stats",
        "DaemonSet/dri-device-plugin",
        "Deployment/traefik",
        "Deployment/authelia",
    }
)

WORKLOAD_PROPERTIES = (
    Property(
        name="sa-less-pods-refuse-the-default-token",
        reason=(
            "A pod that names no ServiceAccount runs as `default`, whose token grants nothing "
            "this cluster's RBAC hands out. A token that grants nothing is still a bearer "
            "credential on a tmpfs in every container. `pod_shell` in "
            "ansible/templates/workload-shell.yml.j2 encodes the rule (no service_account -> "
            "`false`; one named -> no line), so an offence on a Deployment or DaemonSet is a "
            "call that overrides it. A pod that names an SA is left alone: the SA object may set "
            "the field, and the pod's own value then means nothing. Read off the parsed pod "
            "spec, because a grep counts the `automountServiceAccountToken: true` on a "
            "ServiceAccount OBJECT as if it were the pod's."
        ),
        select=_pod_specs,
        offence=_automount_offence,
        red=("nut", {}),
        green=("nut", {"automountServiceAccountToken": False}),
        more_red=(
            ("nut", {"automountServiceAccountToken": True}),
            ("nut", {"automountServiceAccountToken": "false"}),
        ),
        more_green=(
            ("observability", {"serviceAccountName": "prometheus"}),
            (
                "headlamp",
                {
                    "serviceAccountName": "headlamp",
                    "automountServiceAccountToken": True,
                },
            ),
        ),
        min_matches=_MIN_POD_TEMPLATES,
        must_find=_POD_TEMPLATES_MUST_FIND,
    ),
    Property(
        name="pods-disable-service-link-env-vars",
        reason=(
            "Kubernetes injects <NAME>_SERVICE_HOST, <NAME>_PORT_<n>_TCP and so on for every "
            "Service in the namespace, and an app that reads its config from <NAME>_* env vars "
            "takes them as configuration. Authelia did, and exited before serving anything: "
            "`error occurred performing deprecation mapping for keys 'server.host', "
            "'server.port', and 'server.path' to new key server.address: the new key already "
            "exists with value 'tcp4://:9091' but the deprecated keys and the new key can't "
            "both be configured`. A Service name matching an app's env-var prefix is the normal "
            "case in this namespace, so every pod template of every kind sets "
            "enableServiceLinks: false, not just `deployment.yaml.j2`."
        ),
        select=_pod_specs,
        offence=_service_links_offence,
        red=("authelia", {}),
        green=("authelia", {"enableServiceLinks": False}),
        more_red=(
            ("authelia", {"enableServiceLinks": True}),
            ("authelia", {"enableServiceLinks": "false"}),
        ),
        min_matches=_MIN_POD_TEMPLATES,
        must_find=_POD_TEMPLATES_MUST_FIND,
    ),
    Property(
        name="service-macro-places-the-namespace",
        reason=(
            "observability's grafana, kube-state-metrics and otel-collector Services were "
            "hand-written only because the `service()` macro hard-coded `k8s_namespace` "
            "(#3355). They now call it with `namespace=k8s_observability_namespace`, and nut "
            "and karakeep pass no namespace, so they keep `homelab`. A Service in the wrong "
            "namespace parses, schema-checks and applies cleanly beside a Deployment it can "
            "never select, so the manifest validator cannot see a dropped argument."
        ),
        select=_macro_services,
        offence=_service_namespace_offence,
        red=(OBSERVABILITY, {"metadata": {"namespace": "homelab"}}),
        green=("nut", {"metadata": {"namespace": "homelab"}}),
        must_find=frozenset(
            {f"{OBSERVABILITY}/{name}" for name in OBSERVABILITY_MACRO_SERVICES}
            | {"nut/nut"}
        ),
    ),
    Property(
        name="built-images-pull-always",
        reason=(
            "image-builder pushes every build as the mutable `localhost:5000/<name>:latest` and "
            "an immutable `:sha-<12 hex>` content tag. A rebuild whose inputs are unchanged keeps "
            "the same content tag, and `:latest` is the fallback a deploy takes when it skips the "
            "build. Both leave a pod asking the node for a name it already holds, and under "
            "`IfNotPresent` the pod rolls, reads Ready and runs the OLD bytes until the drift "
            "gate in post_tasks/k8s_image_drift_gate.yml fails the deploy. The policy is pinned "
            "explicitly because the API server defaults it only at CREATE time: a Deployment "
            "created with an upstream `@sha256:` pin keeps `IfNotPresent` through every later "
            "apply that changes only `image`."
        ),
        select=_built_image_containers,
        offence=_pull_always_offence,
        red=(
            "nut",
            {"image": "localhost:5000/nut:latest", "imagePullPolicy": "IfNotPresent"},
        ),
        green=(
            "nut",
            {"image": "localhost:5000/nut:latest", "imagePullPolicy": "Always"},
        ),
        # Seven roles include k8s/image-builder, several with more than one built container.
        min_matches=6,
    ),
    Property(
        name="rendered-kinds-have-vendored-schemas",
        reason=(
            "validate/k8s_manifests.py reports an object with no schema as skipped and lets it "
            "through, so a manifest in a new API group, or a new CRD kind, would arrive checked "
            "by nothing. scripts/validate/refresh_vendored_schemas.py refreshes the schemas."
        ),
        select=_every_object,
        offence=_no_schema_offence,
        red=(
            "nut",
            {
                "apiVersion": "autoscaling/v2",
                "kind": "HorizontalPodAutoscaler",
                "metadata": {"name": "x"},
            },
        ),
        green=(
            "nut",
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "x"}},
        ),
        # One kind per vendored core group the tree uses, plus each CRD.
        must_find=frozenset(
            {
                "v1 Service",
                "apps/v1 Deployment",
                "batch/v1 CronJob",
                "discovery.k8s.io/v1 EndpointSlice",
                "networking.k8s.io/v1 NetworkPolicy",
                "rbac.authorization.k8s.io/v1 Role",
                "storage.k8s.io/v1 StorageClass",
                "traefik.io/v1alpha1 IngressRoute",
            }
        ),
    ),
    Property(
        name="karakeep-chrome-write-paths-are-emptydirs",
        reason=(
            "karakeep-chrome runs on a read-only root, and headless chromium writes to `/tmp` "
            "(profile, crash dumps, the shm files `--disable-dev-shm-usage` moves out of "
            "/dev/shm) and `/var/cache/fontconfig`. A missing /tmp breaks rendering; a missing "
            "fontconfig cache only logs `Fontconfig error: No writable cache directories` and "
            "rescans the font tree on every process start. test_container_security_context.py "
            "reads the securityContext without asking what the container then needs writable. "
            "Add a path here when a chromium flag makes it write somewhere new."
        ),
        select=_karakeep_chrome,
        offence=_chrome_writable_offence,
        red=(
            "karakeep",
            _chrome_deployment(
                {
                    "name": "chrome-fontconfig-cache",
                    "persistentVolumeClaim": {"claimName": "karakeep-fontconfig"},
                }
            ),
        ),
        green=(
            "karakeep",
            _chrome_deployment({"name": "chrome-fontconfig-cache", "emptyDir": {}}),
        ),
        must_find=frozenset({KARAKEEP_CHROME}),
    ),
    Property(
        name="homepage-revalidates-on-start",
        reason=(
            "The homepage image ships a build-time render of `/` that carries no settings: "
            "`getStaticProps` has no `revalidate` key, so `next build` bakes the page and "
            "nothing expires it. Upstream re-renders only when a browser's stored `/api/hash` "
            "MISMATCHES the pod's, so a fresh pod visited by fresh browsers serves the "
            "config-less page for its whole life, at 1/1 and with `probe.py health homepage` "
            "exiting 0. The `lifecycle.postStart` hook calling `/api/revalidate` removes that "
            "state. ansible/roles/k8s/homepage/CLAUDE.md has the measurements."
        ),
        select=_homepage_deployment,
        offence=_startup_revalidation_offence,
        # Calls the endpoint but can fail, and a failing postStart kills the container.
        red=(
            "homepage",
            _homepage_with_post_start(
                [
                    "sh",
                    "-c",
                    "wget -q -O /dev/null http://127.0.0.1:3000/api/revalidate",
                ]
            ),
        ),
        green=(
            "homepage",
            _homepage_with_post_start(
                [
                    "sh",
                    "-c",
                    'i=0; while [ "$i" -lt 60 ]; do wget -q -O /dev/null '
                    '"http://127.0.0.1:3000/api/revalidate" && break; i=$((i+1)); sleep 1; '
                    "done; exit 0",
                ]
            ),
        ),
        must_find=frozenset({"homepage"}),
    ),
)
