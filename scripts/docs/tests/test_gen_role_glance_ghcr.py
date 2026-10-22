"""The role glance names GHCR for a flux-owned built image (#5462).

A flux-owned role's built image is pulled from GHCR at its content tag (D5.4b), so its Image
line names the GHCR repository. The Ansible default names the in-cluster registry, which only
an Ansible-owned render still pulls from.
"""

import pytest

import gen_role_glance as g
from renderer.renderer_lib import images


def _image_line(name: str, *, flux: bool) -> str:
    """The unwrapped Image(s) line `glance_lines` prints for the live role `name`."""

    assert name in images.builder_roles(), f"{name} builds no image"
    entry = dict(g.Estate().k8s_entries()[name])
    entry.pop("owner", None)
    if flux:
        entry["owner"] = "flux"
    lines = g.glance_lines(
        entry,
        g.K8S_ROLES / name,
        group_vars=g.Estate().group_vars,
        owner="flux" if flux else None,
    )
    (line,) = [ln for ln in lines if ln.startswith(("- **Image:**", "- **Images:**"))]
    return line


@pytest.mark.parametrize("name", ["ical-proxy", "homelab-mcp"])
def test_a_flux_owned_built_image_glance_names_its_ghcr_repository(name):

    line = _image_line(name, flux=True)
    assert f"`{images.GHCR}/{name}`" in line, line
    assert "<k8s_registry_pull_host>" not in line, line


def test_the_image_line_follows_the_owner_of_the_same_built_image():

    ansible_line = _image_line("terraria", flux=False)
    assert "`<k8s_registry_pull_host>/terraria`" in ansible_line, ansible_line
    flux_line = _image_line("terraria", flux=True)
    assert f"`{images.GHCR}/terraria`" in flux_line, flux_line
    assert "<k8s_registry_pull_host>" not in flux_line, flux_line


def test_a_flux_owned_role_swaps_only_its_built_image_for_ghcr():
    # valheim pins an upstream server image beside the mods image it builds.

    line = _image_line("valheim", flux=True)
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
