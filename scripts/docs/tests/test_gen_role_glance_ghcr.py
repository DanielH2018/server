"""The role glance names GHCR for a flux-owned built image (#5462).

A flux-owned role's built image is pulled from GHCR at its content tag (D5.4b), so its Image
line names the GHCR repository. The Ansible default names the in-cluster registry, which only
an Ansible-owned render still pulls from.
"""

import shutil
from pathlib import Path

import pytest
import yaml

import gen_role_glance as g
from lib.estate import Inventory
from renderer.renderer_lib import images


def _image_line(tmp_path: Path, name: str, *, flux: bool) -> str:
    """The unwrapped Image(s) line `stale_k8s_docs` writes for a copy of the live role `name`.

    The copy's entry carries `owner: flux` when `flux` is set and no `owner` otherwise, so
    one role renders either way whatever its live entry says.
    """
    assert name in images.builder_roles(), f"{name} builds no image"
    entry = dict(g.Estate().k8s_entries()[name])
    entry.pop("owner", None)
    if flux:
        entry["owner"] = "flux"
    roles = tmp_path / "roles"
    # The whole role, so its `k8s/image-builder` include and build context come along.
    shutil.copytree(g.K8S_ROLES / name, roles / name)
    (tmp_path / "daniel-box.yml").write_text(
        yaml.safe_dump({"containers_list": [entry]})
    )
    estate = g.Estate(Inventory(host_vars=tmp_path))
    g.stale_k8s_docs(write=True, estate=estate, k8s_roles=roles)
    doc = (roles / name / "CLAUDE.md").read_text()
    block = doc.split(g.END)[0].split(g.BEGIN)[-1]
    bullets = [" ".join(b.split()) for b in block.split("\n- ")]
    (line,) = [b for b in bullets if b.startswith(("**Image:**", "**Images:**"))]
    return line


@pytest.mark.parametrize("name", ["ical-proxy", "homelab-mcp"])
def test_a_flux_owned_built_image_glance_names_its_ghcr_repository(tmp_path, name):

    line = _image_line(tmp_path, name, flux=True)
    assert f"`{images.GHCR}/{name}`" in line, line
    assert "<k8s_registry_pull_host>" not in line, line


def test_the_image_line_follows_the_owner_of_the_same_built_image(tmp_path):

    ansible_line = _image_line(tmp_path / "ansible", "terraria", flux=False)
    assert "`<k8s_registry_pull_host>/terraria`" in ansible_line, ansible_line
    flux_line = _image_line(tmp_path / "flux", "terraria", flux=True)
    assert f"`{images.GHCR}/terraria`" in flux_line, flux_line
    assert "<k8s_registry_pull_host>" not in flux_line, flux_line


def test_a_flux_owned_role_swaps_only_its_built_image_for_ghcr(tmp_path):
    # valheim pins an upstream server image beside the mods image it builds.

    line = _image_line(tmp_path, "valheim", flux=True)
    assert "`ghcr.io/community-valheim-tools/valheim-server`" in line, line
    assert f"`{images.GHCR}/valheim`" in line, line
    assert "<k8s_registry_pull_host>" not in line, line


def test_every_committed_flux_owned_builder_glance_names_ghcr():
    """The committed docs carry the regenerated line, not only a fresh render."""

    entries = g.Estate().k8s_entries()
    flux_builders = [
        r for r in images.builder_roles() if entries.get(r, {}).get("owner") == "flux"
    ]
    assert {"ical-proxy", "homelab-mcp"} <= set(flux_builders), flux_builders
    wrong = []
    for name in flux_builders:
        doc = (g.K8S_ROLES / name / "CLAUDE.md").read_text()
        block = " ".join(doc.split(g.END)[0].split(g.BEGIN)[-1].split())
        if (
            f"`{images.GHCR}/{name}`" not in block
            or "<k8s_registry_pull_host>" in block
        ):
            wrong.append(name)
    assert wrong == [], f"glance still names the in-cluster registry in {wrong}"
