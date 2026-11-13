"""The role glance names GHCR for a k8s role's built image (#5462).

A k8s role's built image is pulled from GHCR at its content tag (D5.4b), so its Image line
names the GHCR repository rather than the in-cluster registry its defaults name.
"""

import shutil
from pathlib import Path

import pytest
import yaml

import gen_role_glance as g
from lib.estate import Inventory
from renderer.renderer_lib import images


def _image_line(tmp_path: Path, name: str) -> str:
    """The unwrapped Image(s) line `stale_k8s_docs` writes for a copy of the live role `name`.

    The copy's entry carries no `owner`, so a service-list entry alone must select the GHCR repository.
    """
    assert name in images.builder_roles(), f"{name} builds no image"
    entry = dict(g.Estate().k8s_entries()[name])
    entry.pop("owner", None)
    roles = tmp_path / "roles"
    # The whole role, so its `k8s/image-builder` include and build context come along.
    shutil.copytree(g.K8S_ROLES / name, roles / name)
    services = tmp_path / "services.yml"
    services.write_text(yaml.safe_dump({"containers_list": [entry]}))
    estate = g.Estate(Inventory(services=services))
    g.stale_k8s_docs(write=True, estate=estate, k8s_roles=roles)
    doc = (roles / name / "CLAUDE.md").read_text()
    block = doc.split(g.END)[0].split(g.BEGIN)[-1]
    bullets = [" ".join(b.split()) for b in block.split("\n- ")]
    (line,) = [b for b in bullets if b.startswith(("**Image:**", "**Images:**"))]
    return line


@pytest.mark.parametrize("name", ["ical-proxy", "homelab-mcp", "terraria"])
def test_a_flux_owned_built_image_glance_names_its_ghcr_repository(tmp_path, name):

    line = _image_line(tmp_path, name)
    assert f"`{images.GHCR}/{name}`" in line, line
    assert "<k8s_registry_pull_host>" not in line, line


def test_a_flux_owned_role_swaps_only_its_built_image_for_ghcr(tmp_path):
    # valheim pins an upstream server image beside the mods image it builds.

    line = _image_line(tmp_path, "valheim")
    assert "`ghcr.io/community-valheim-tools/valheim-server`" in line, line
    assert f"`{images.GHCR}/valheim`" in line, line
    assert "<k8s_registry_pull_host>" not in line, line


def test_built_image_names_skips_a_retired_roles_pycache_shell(tmp_path):
    # A deleted role leaves `<role>/tests/__pycache__/` on disk, which git never removes (#7917).
    shutil.copytree(g.K8S_ROLES / "valheim", tmp_path / "valheim")
    debris = tmp_path / "retired" / "tests" / "__pycache__"
    debris.mkdir(parents=True)
    (debris / "test_retired.cpython-312.pyc").write_bytes(b"")

    assert "valheim" in g.built_image_names(tmp_path)
