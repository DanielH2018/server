"""The `reconcile` component rendered as a host package, the way every host package ships it.

This renders a fixture host package that lists the component alone, so the tests judge the
component rather than whatever else `hosts/daniel-pi/package.yml` ships. It renders through
`render_host`, the function `homelab-render host` calls, because the component ships an enable
link and a shared package refuses one. The fixture tree links the repo's real component and
`hosts/lib/`, and the renderer resolves those links before its containment checks, so the
checks judge the real files.
"""

from pathlib import Path

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

HOST = "daniel-pi"


def render_reconcile(root: Path) -> host_mode.Package:
    """Render the fixture host package under `root`, an empty directory, and return it."""
    hosts = root / "hosts"
    (hosts / "components").mkdir(parents=True)
    (hosts / "components" / "reconcile").symlink_to(REPO / "hosts/components/reconcile")
    (hosts / "lib").symlink_to(REPO / "hosts/lib")
    spec = hosts / HOST / "package.yml"
    spec.parent.mkdir(parents=True)
    spec.write_text("components: [reconcile]\n")
    return host_mode.render_host(HOST, hosts_root=hosts)
