"""The `reconcile` component rendered as a package, the way a host package will ship it.

No package lists the component until H3.6's `hosts/daniel-pi/package.yml`. This renders a
fixture shared package that lists it, through `render_package`, the function `homelab-render
package` calls. The fixture tree links the repo's real component and `hosts/lib/`, and the
renderer resolves those links before its containment checks, so the checks judge the real
files.
"""

from pathlib import Path

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

PACKAGE = "homelab-reconcile-fixture"


def render_reconcile(root: Path) -> host_mode.Package:
    """Render the fixture package under `root`, an empty directory, and return it."""
    hosts = root / "hosts"
    (hosts / "components").mkdir(parents=True)
    (hosts / "components" / "reconcile").symlink_to(REPO / "hosts/components/reconcile")
    (hosts / "lib").symlink_to(REPO / "hosts/lib")
    spec = hosts / "packages" / PACKAGE / "package.yml"
    spec.parent.mkdir(parents=True)
    spec.write_text("components: [reconcile]\n")
    return host_mode.render_package(PACKAGE, hosts_root=hosts)
