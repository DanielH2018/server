"""Render daniel-box's checkout-timer components into a fixture package (H7.36).

`hosts/daniel-box/package.yml` lists the components the operator has armed; the tests for
`secret-rotation` and `loki-route-witness` must render those two whatever the live spec says, and
a package that names a credential needs the `secrets-unit` component and a ciphertext. So the
fixture lays a `hosts/` tree under `tmp_path` that links the repo's own `components/` and `lib/`
directories, lists the components under test, and carries a placeholder ciphertext. No real key,
ciphertext or value is read: the renderer reads the key NAMES of `ansible/vars/secrets.yml` only.
"""

from pathlib import Path

import yaml

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

HOST = "daniel-box"
UNITS = "/usr/lib/systemd/system"
LIB = "/usr/lib/homelab"
# Every checkout-timer component a test here renders, with the ones they source from.
BASE = ("secrets-unit", "kuma-push", "check-lib")


def render_fixture(tmp_path: Path, components: tuple[str, ...]) -> host_mode.Package:
    """`daniel-box`'s package with `BASE` and `components` listed, rendered from the repo's sources."""
    root = tmp_path / "hosts"
    (root / HOST).mkdir(parents=True)
    # A `shared` entry resolves against the tree's root, one level above `hosts/`.
    (tmp_path / "ansible").symlink_to(REPO / "ansible")
    (root / "components").symlink_to(REPO / "hosts" / "components")
    (root / "lib").symlink_to(REPO / "hosts" / "lib")
    spec = {"components": [*BASE, *components]}
    (root / HOST / "package.yml").write_text(yaml.safe_dump(spec))
    sops_dir = tmp_path / "host-sops"
    sops_dir.mkdir()
    (sops_dir / f"{HOST}.sops.yaml").write_text("sops:\n  version: fixture\n")
    return host_mode.render_host(HOST, root, sops_dir=sops_dir)


def text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()
