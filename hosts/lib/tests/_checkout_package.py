"""Render daniel-box's package from a chosen set of components, for the checkout-timer tests.

`hosts/daniel-box/package.yml` lists every component the host installs and a credential-bearing
one needs the host's SOPS file, which is not part of what these tests hold. A test therefore
renders a throwaway package that lists only the components under test, over symlinks to the real
`hosts/components`, `hosts/lib` and `ansible/` trees, so it reads the same sources the real
package does.
"""

from pathlib import Path

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

# The components every checkout timer's package carries beside its own: the secrets unit that
# fills the credentials, and the push and check libraries its script sources.
BASE_COMPONENTS = ("secrets-unit", "kuma-push", "check-lib")


def render_daniel_box(tmp_path: Path, *components: str) -> host_mode.Package:
    """daniel-box's package with `BASE_COMPONENTS` and `components`, rendered from the tree."""
    hosts = tmp_path / "hosts"
    (hosts / "daniel-box").mkdir(parents=True)
    (hosts / "components").symlink_to(REPO / "hosts" / "components")
    (hosts / "lib").symlink_to(REPO / "hosts" / "lib")
    (tmp_path / "ansible").symlink_to(REPO / "ansible")
    names = ", ".join((*BASE_COMPONENTS, *components))
    (hosts / "daniel-box" / "package.yml").write_text(
        f"description: test package\ncomponents: [{names}]\n"
    )
    # The renderer carries the ciphertext into the package without reading it, so a placeholder
    # stands in for daniel-box's SOPS file, which this tree does not hold yet.
    sops = tmp_path / "sops"
    sops.mkdir()
    (sops / "daniel-box.sops.yaml").write_text("placeholder: not a secret\n")
    return host_mode.render_host("daniel-box", hosts_root=hosts, sops_dir=sops)
