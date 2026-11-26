"""The k8s services the cluster renders, derived from `k8s/services.yml` and the role files.

A service renders when its role has something to render: its own manifest template, a shared
default its entry earns, or a vendored manifest. `render_targets` keeps a tag Flux owns,
because the bundle publishes it.
"""

from pathlib import Path

from lib.k8s_roles import MANIFEST_TEMPLATE_GLOB, SHARED_DEFAULT_TRIGGERS
from lib.render_guard import entry_tags, k8s_services
from lib.repo_paths import K8S_ROLES, K8S_SERVICES
from renderer.renderer_lib.bundle import PATCHES_DIR

# Vendored files keep these `files/` subdirectories under `cluster/<tag>/`: upstream manifests byte
# for byte, and Kustomize patches.
VENDORED_DIRS = ("upstream", PATCHES_DIR)


def vendored_paths(role_dir: Path) -> list[tuple[str, Path]]:
    """`role_dir`'s vendored manifests, each with the `files/` subdirectory that holds it."""
    return [
        (sub, path)
        for sub in VENDORED_DIRS
        for path in sorted((role_dir / "files" / sub).glob("*.y*ml"))
        if path.is_file()
    ]


def renders(entry: dict, roles: Path = K8S_ROLES) -> bool:
    """Whether the role `entry` names has a manifest template, shared default or vendored file."""
    role_dir = roles / entry["name"]
    return (
        any((role_dir / "templates").glob(MANIFEST_TEMPLATE_GLOB))
        or any(key in entry for key in SHARED_DEFAULT_TRIGGERS.values())
        or bool(vendored_paths(role_dir))
    )


def render_targets(
    services: Path = K8S_SERVICES,
    roles: Path = K8S_ROLES,
) -> list[str]:
    """The sorted service names the cluster renders, each its own tag.

    Empty when the service list `services` is missing.
    """
    return sorted(
        entry["name"]
        for entry in k8s_services(services)
        if entry["name"] in entry_tags(entry) and renders(entry, roles)
    )
