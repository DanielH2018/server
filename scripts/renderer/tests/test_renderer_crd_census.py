"""D5's CRD census: no CustomResourceDefinition renders in a tag whose Kustomization prunes.

Under `prune: true`, a render that dropped a CRD file makes Flux delete that CRD, and the API
server then deletes every custom object of its kind. For traefik's CRDs that is every
IngressRoute and Middleware, so the edge goes down. A tag that renders a CRD therefore sets
H1.1a's `flux_prune: false` in its defaults, which `role_fields` reads and the bundle writes as
`prune: false`. The lever reaches `<tag>` alone, so a CRD in a `<tag>-deferred` directory is
always under prune (`bundle.flux_files`, operator ruling of 2026-10-22). The census reads the
same lever for every render target, Ansible-owned ones included, because a flip carries the
render as it stands. `traefik-crds` and `flux` are the named members the live render must
find. This is D6.19's CRD guard landing early, without D6's exception for `flux`. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, step 26's traefik bullet and step 29.

Run: uv run pytest scripts/renderer/tests/test_renderer_crd_census.py
"""

import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from deploy_tools.render_targets import render_targets
from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from lib.exit_codes import RENDER_OK
from lib.k8s_roles import K8S_ROLES, role_fields
from lib.repo_paths import HOST_VARS
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE, KUSTOMIZATION
from renderer.renderer_lib.cluster import flux_owned_tags
from renderer.renderer_lib.ordering import split_kind

DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
CRD = (
    "apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\n"
    "metadata:\n  name: {name}\n"
)
# Each named member with a CRD its render must hold, so the census cannot pass on nothing.
MEMBERS = {
    "traefik-crds": "ingressroutes.traefik.io",
    "flux": "kustomizations.kustomize.toolkit.fluxcd.io",
}


def crds_by_tag(directories: Mapping[str, Mapping[str, str]]) -> dict[str, set[str]]:
    """Each Kustomization's directory mapped to the CRDs it renders; one with none is left out.

    A per-tag kind's directory, such as `<tag>-deferred`, keeps its own name, because its
    Kustomization takes no `prune` from the role (`pruned_crds`).
    """
    found: dict[str, set[str]] = {}
    for directory, files in directories.items():
        for name, text in files.items():
            if name == KUSTOMIZATION:
                continue
            for doc in yaml_fast.safe_load_all(text):
                if (
                    isinstance(doc, dict)
                    and doc.get("kind") == "CustomResourceDefinition"
                ):
                    found.setdefault(directory, set()).add(str(doc["metadata"]["name"]))
    return found


def pruned_crds(
    crds: Mapping[str, set[str]], fields: Mapping[str, Mapping]
) -> dict[str, list[str]]:
    """Each directory that renders a CRD and whose Kustomization renders `prune: true`.

    Only `<tag>` itself takes the role's `prune`; every per-tag kind's directory prunes.
    """
    return {
        name: sorted(names)
        for name, names in sorted(crds.items())
        if split_kind(name)[1] or fields.get(name, {}).get("prune", True)
    }


def _render(out: Path, tmp_path: Path, roles: list[str] | None = None) -> dict:
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    assert (
        homelab_render.cluster(out, roles, None, cluster_file=cluster_file) == RENDER_OK
    )
    return {
        d.name: {
            str(f.relative_to(d)): f.read_text() for f in d.rglob("*") if f.is_file()
        }
        for d in (out / "cluster").iterdir()
    }


def test_a_crd_in_a_tag_that_prunes_is_flagged():
    crds = crds_by_tag({"one": {"crd.yaml": CRD.format(name="widgets.example.io")}})
    assert pruned_crds(crds, {}) == {"one": ["widgets.example.io"]}


def test_a_crd_in_a_tag_with_prune_false_is_clean():
    crds = crds_by_tag({"one": {"crd.yaml": CRD.format(name="widgets.example.io")}})
    assert pruned_crds(crds, {"one": {"prune": False}}) == {}


def test_a_crd_in_a_deferred_directory_of_a_tag_with_prune_false_is_flagged():
    """`flux_prune` reaches `<tag>` alone, so `<tag>-deferred` prunes whatever the role sets."""
    crds = crds_by_tag(
        {"one-deferred": {"crd.yaml": CRD.format(name="widgets.example.io")}}
    )
    assert pruned_crds(crds, {"one": {"prune": False}}) == {
        "one-deferred": ["widgets.example.io"]
    }


@pytest.mark.parametrize("member", sorted(MEMBERS))
def test_a_member_with_flux_prune_removed_is_flagged(tmp_path, member):
    # The red fixture: the member's real render, and its real defaults minus the lever.
    directories = _render(tmp_path / "out", tmp_path, [member])
    roles = tmp_path / "roles"
    shutil.copytree(K8S_ROLES / member, roles / member)
    defaults = roles / member / "defaults" / "main.yml"
    text = defaults.read_text()
    assert "\nflux_prune: false" in text
    defaults.write_text(text.replace("\nflux_prune: false", "\n"))
    fields, problems = role_fields([member], roles)
    assert problems == []
    [(name, names)] = pruned_crds(crds_by_tag(directories), fields).items()
    assert name == member
    assert MEMBERS[member] in names


def test_no_render_target_renders_a_crd_under_prune(tmp_path):
    directories = _render(tmp_path / "out", tmp_path)
    assert len(directories) >= 50
    targets = set(render_targets(K8S_HOST, HOST_VARS))
    assert targets <= {split_kind(d)[0] for d in directories}
    crds = crds_by_tag(directories)
    fields, problems = role_fields(targets)
    assert problems == []
    # Non-vacuity: each named member renders its kinds, is flux-owned, and its lever is off.
    assert set(MEMBERS) <= flux_owned_tags()
    for member, crd in MEMBERS.items():
        assert crd in crds.get(member, set()), member
        assert fields[member].get("prune") is False, member
    assert pruned_crds(crds, fields) == {}
