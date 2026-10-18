"""D5's CRD census: no CustomResourceDefinition renders in a tag whose Kustomization prunes.

Under `prune: true`, a render that dropped a CRD file makes Flux delete that CRD, and the API
server then deletes every custom object of its kind. For traefik's CRDs that is every
IngressRoute and Middleware, so the edge goes down. A tag that renders a CRD therefore sets
H1.1a's `flux_prune: false` in its defaults, which `role_fields` reads and the bundle writes as
`prune: false`. The census reads the same lever for every render target, Ansible-owned ones
included, because a flip carries the render as it stands. `traefik-crds` is the named member
the live render must find. This is D6.19's CRD guard landing early, without D6's exception for
`flux`. The plan is docs/target-architecture/deploy/d5-bulk-move.md, step 26's traefik bullet.

Run: uv run pytest scripts/renderer/tests/test_renderer_crd_census.py
"""

import shutil
from collections.abc import Mapping
from pathlib import Path

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
MEMBER = "traefik-crds"
# Each Ansible-owned tag that renders CRDs under prune until its own flip sets the lever,
# mapped to the reason. Ansible never prunes, so the render's prune value is inert until then.
# An entry stops applying the moment its tag is flux-owned, so its flip fails the census
# without `flux_prune: false`, and the flip PR deletes the entry.
UNTIL_FLIP: dict[str, str] = {
    "flux": "Flux's own CRDs; the flux PR sets flux_prune: false (D5's last three PRs)",
}


def crds_by_tag(directories: Mapping[str, Mapping[str, str]]) -> dict[str, set[str]]:
    """Each deploy tag mapped to the CRDs its directories render; a tag with none is left out."""
    found: dict[str, set[str]] = {}
    for directory, files in directories.items():
        tag = split_kind(directory)[0]
        for name, text in files.items():
            if name == KUSTOMIZATION:
                continue
            for doc in yaml_fast.safe_load_all(text):
                if (
                    isinstance(doc, dict)
                    and doc.get("kind") == "CustomResourceDefinition"
                ):
                    found.setdefault(tag, set()).add(str(doc["metadata"]["name"]))
    return found


def pruned_crds(
    crds: Mapping[str, set[str]], fields: Mapping[str, Mapping]
) -> dict[str, list[str]]:
    """Each tag that renders a CRD and whose `fields` leave `prune` at its default of true."""
    return {
        tag: sorted(names)
        for tag, names in sorted(crds.items())
        if fields.get(tag, {}).get("prune", True)
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
    crds = crds_by_tag(
        {"one-deferred": {"crd.yaml": CRD.format(name="widgets.example.io")}}
    )
    assert crds == {"one": {"widgets.example.io"}}
    assert pruned_crds(crds, {"one": {"prune": False}}) == {}


def test_traefik_crds_with_flux_prune_removed_is_flagged(tmp_path):
    # The red fixture: the member's real render, and its real defaults minus the lever.
    directories = _render(tmp_path / "out", tmp_path, [MEMBER])
    roles = tmp_path / "roles"
    shutil.copytree(K8S_ROLES / MEMBER, roles / MEMBER)
    defaults = roles / MEMBER / "defaults" / "main.yml"
    text = defaults.read_text()
    assert "\nflux_prune: false" in text
    defaults.write_text(text.replace("\nflux_prune: false", "\n"))
    fields, problems = role_fields([MEMBER], roles)
    assert problems == []
    [(tag, names)] = pruned_crds(crds_by_tag(directories), fields).items()
    assert tag == MEMBER
    assert any(name == "ingressroutes.traefik.io" for name in names)


def test_no_render_target_renders_a_crd_under_prune(tmp_path):
    directories = _render(tmp_path / "out", tmp_path)
    assert len(directories) >= 50
    targets = set(render_targets(K8S_HOST, HOST_VARS))
    assert targets <= {split_kind(d)[0] for d in directories}
    crds = crds_by_tag(directories)
    # Non-vacuity: the named member renders traefik's kinds, and its lever is off.
    assert {"ingressroutes.traefik.io", "middlewares.traefik.io"} <= crds.get(
        MEMBER, set()
    )
    fields, problems = role_fields(targets)
    assert problems == []
    assert fields[MEMBER].get("prune") is False
    flux_owned = flux_owned_tags()
    exempt = {tag for tag in UNTIL_FLIP if tag not in flux_owned}
    flagged = pruned_crds(crds, fields)
    assert {t: n for t, n in flagged.items() if t not in exempt} == {}
    # An entry whose tag renders no pruned CRD any more is stale.
    assert sorted(set(UNTIL_FLIP) - set(flagged)) == []
