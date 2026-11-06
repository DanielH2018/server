"""The k8s services a host renders, derived from its `containers_list` and the role files.

A service renders when its role has something to render: its own manifest template, a shared
default its entry earns, or a vendored manifest. `render_targets` keeps a tag Flux owns,
because the bundle publishes it.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from pathlib import Path

from lib.k8s_roles import MANIFEST_TEMPLATE_GLOB, SHARED_DEFAULT_TRIGGERS
from lib.render_guard import containers_entries, entry_platform, entry_tags
from lib.repo_paths import HOST_VARS, K8S_ROLES
from renderer.renderer_lib.bundle import PATCHES_DIR

# A role's vendored files sit in these subdirectories of `files/` and keep them under
# `cluster/<tag>/` (H1 *Vendored upstream manifests*): upstream manifests byte for byte, and
# Kustomize patches. H1.1d's `health_vendored.py` reads the same two.
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
    host: str,
    host_vars: Path = HOST_VARS,
    roles: Path = K8S_ROLES,
) -> list[str]:
    """The sorted service names `host` renders, each its own tag.

    Empty for a host whose `host_vars` file is missing: that host declares no services.
    """
    path = host_vars / f"{host}.yml"
    if not path.exists():
        return []
    return sorted(
        entry["name"]
        for entry in containers_entries(path)
        if entry_platform(entry) == "k8s"
        and entry["name"] in entry_tags(entry)
        and renders(entry, roles)
    )
