"""Rendered-manifest property rows about how a container's image is pulled, and what it needs.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

from _k8s_render import pod_spec
from _property_table import Property
from _workload_property_rows import POD_KINDS

_DIGEST = "@sha256:"


def _digest_pinned_containers(role: str, tpl: str, doc: dict):
    if doc.get("kind") not in POD_KINDS:
        return
    spec = pod_spec(doc)
    for container in (spec.get("initContainers") or []) + (
        spec.get("containers") or []
    ):
        if _DIGEST in str(container.get("image", "")):
            yield f"{doc['metadata']['name']}:{container['name']}", container


def _pull_if_not_present_offence(role: str, container: dict) -> str | None:
    policy = container.get("imagePullPolicy")
    if policy == "IfNotPresent":
        return None
    return (
        f"imagePullPolicy is {policy!r} on a digest-pinned image, so a restart with the "
        "registry unreachable cannot start the container the node already holds. Add "
        "`imagePullPolicy: IfNotPresent` next to `image:`"
    )


def _karakeep_app(role: str, tpl: str, doc: dict):
    if role == "karakeep" and doc.get("kind") == "Deployment":
        if doc["metadata"]["name"] == "karakeep":
            yield "karakeep", doc


def _s6_run_offence(role: str, doc: dict) -> str | None:
    spec = pod_spec(doc)
    empty_dirs = {v["name"] for v in spec.get("volumes", []) if "emptyDir" in v}
    for c in spec.get("containers", []):
        if c["name"] == "karakeep":
            for m in c.get("volumeMounts", []):
                if m["mountPath"] == "/run" and m["name"] in empty_dirs:
                    return None
    return (
        "/run is the image's node-owned directory, which s6 cannot chown without CHOWN"
    )


def _karakeep_with_run(volume: dict) -> dict:
    mount = {"name": "karakeep-run", "mountPath": "/run"}
    container = {"name": "karakeep", "volumeMounts": [mount]}
    pod = {"volumes": [volume], "containers": [container]}
    return {"spec": {"template": {"spec": pod}}}


def _karakeep_env(doc: dict) -> dict[str, str]:
    for c in pod_spec(doc).get("containers", []):
        if c["name"] == "karakeep":
            return {e["name"]: e.get("value", "") for e in c.get("env", [])}
    return {}


def _trusted_origins_offence(role: str, doc: dict) -> str | None:
    env = _karakeep_env(doc)
    raw = env.get("AUTH_TRUSTED_ORIGINS")
    if raw is None:
        return "AUTH_TRUSTED_ORIGINS is unset, so the app trusts upstream's default `*`"
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins or any("*" in o for o in origins):
        return f"AUTH_TRUSTED_ORIGINS is {raw!r}, which is empty or holds a wildcard"
    own = env.get("NEXTAUTH_URL", "")
    domain = own.removeprefix("https://").partition(".")[2]
    if own not in origins:
        return f"AUTH_TRUSTED_ORIGINS {raw!r} omits NEXTAUTH_URL {own!r}"
    stray = [
        o
        for o in origins
        if not (o.startswith("https://") and o.endswith("." + domain))
    ]
    if stray:
        return f"AUTH_TRUSTED_ORIGINS holds {stray!r}, outside https://*.{domain}"
    return None


def _karakeep_with_env(env: dict[str, str]) -> dict:
    container = {
        "name": "karakeep",
        "env": [{"name": k, "value": v} for k, v in env.items()],
    }
    return {"spec": {"template": {"spec": {"containers": [container]}}}}


_OWN_URL = "https://karakeep.example.test"
_LOCAL_URL = "https://karakeep.local.example.test"


IMAGE_PROPERTIES = (
    Property(
        name="karakeep-auth-trusted-origins-are-its-own-names",
        reason=(
            "better-auth accepts a sign-in only from NEXTAUTH_URL and AUTH_TRUSTED_ORIGINS, and "
            "upstream defaults the latter to `*`, which trusts whatever address the request "
            "arrived on (packages/shared/config.ts). The Deployment lists the public and "
            "`.local.` names the role routes instead, built from the same variables as "
            "NEXTAUTH_URL (#5781). Each origin must be https and under NEXTAUTH_URL's domain."
        ),
        select=_karakeep_app,
        offence=_trusted_origins_offence,
        red=(
            "karakeep",
            _karakeep_with_env({"NEXTAUTH_URL": _OWN_URL, "AUTH_TRUSTED_ORIGINS": "*"}),
        ),
        more_red=(
            ("karakeep", _karakeep_with_env({"NEXTAUTH_URL": _OWN_URL})),
            (
                "karakeep",
                _karakeep_with_env(
                    {
                        "NEXTAUTH_URL": _OWN_URL,
                        "AUTH_TRUSTED_ORIGINS": f"{_OWN_URL},https://other.invalid",
                    }
                ),
            ),
        ),
        green=(
            "karakeep",
            _karakeep_with_env(
                {
                    "NEXTAUTH_URL": _OWN_URL,
                    "AUTH_TRUSTED_ORIGINS": f"{_OWN_URL},{_LOCAL_URL}",
                }
            ),
        ),
        must_find=frozenset({"karakeep"}),
    ),
    Property(
        name="karakeep-run-is-an-emptydir",
        reason=(
            "karakeep's PID 1 is s6-overlay's /init at uid 0 with every capability dropped. "
            "Since 0.34.0 the image ships /run owned by node (karakeep-app/karakeep#3114), and "
            "s6's preinit chowns /run whenever its owner differs from the container uid. "
            "Without CHOWN that exits 111 before the app starts, which took karakeep down on "
            "2026-10-26 (#6387). A root-owned emptyDir at /run gives preinit nothing to chown."
        ),
        select=_karakeep_app,
        offence=_s6_run_offence,
        red=("karakeep", _karakeep_with_run({"name": "karakeep-run", "hostPath": {}})),
        green=(
            "karakeep",
            _karakeep_with_run({"name": "karakeep-run", "emptyDir": {}}),
        ),
        must_find=frozenset({"karakeep"}),
    ),
    Property(
        name="digest-pinned-images-pull-if-not-present",
        reason=(
            "A digest is immutable, so pulling it again buys nothing. Kubernetes defaults the "
            "policy to `Always` for a `:latest` tag whatever digest follows it, and the API "
            "server applies that default only at CREATE time: a Deployment created as "
            "`:latest@sha256:` keeps `Always` after a retag that leaves the field unset. So the "
            "policy is pinned explicitly on every digest ref, not only on `:latest` ones. "
            "On 2026-10-22 the WAN was down 17:01-18:22 UTC and qbittorrent "
            "(`lscr.io/linuxserver/qbittorrent:latest@sha256:`) restarted into 71 minutes of "
            "ImagePullBackOff with the digest already on the node (#6136). Built images "
            "(`localhost:`) carry no digest and keep `Always`, per `built-images-pull-always`."
        ),
        select=_digest_pinned_containers,
        offence=_pull_if_not_present_offence,
        red=(
            "qbittorrent",
            {"image": "lscr.io/linuxserver/qbittorrent:latest@sha256:ab"},
        ),
        green=(
            "qbittorrent",
            {
                "image": "lscr.io/linuxserver/qbittorrent:latest@sha256:ab",
                "imagePullPolicy": "IfNotPresent",
            },
        ),
        more_red=(
            # A versioned tag defaults to IfNotPresent only on create, so it is pinned too.
            ("pihole", {"image": "pihole/pihole:2026.09.0@sha256:ab"}),
            (
                "peanut",
                {
                    "image": "brandawg93/peanut:latest@sha256:ab",
                    "imagePullPolicy": "Always",
                },
            ),
        ),
        # 133 digest-pinned containers render. Close enough to notice a contraction.
        min_matches=120,
        must_find=frozenset(
            {
                "qbittorrent:qbittorrent",
                "peanut:peanut",
                "cloudflare-ddns-direct:cloudflare-ddns",
            }
        ),
    ),
)
