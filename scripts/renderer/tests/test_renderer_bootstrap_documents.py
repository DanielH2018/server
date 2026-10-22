"""The documents the bootstrap procedure applies by hand (D5 step 28, R4 PR 7b).

Scope of "applied by hand": the OCIRepository and the root from `cluster/flux-deferred/` (steps 5
and 9) and every `flux/<tag>.yaml` Kustomization (step 6). Step 3 applies `components.yaml`
alone, and the test does not scan it: `components.yaml` quotes `${var:=default}` in a CRD
description. `cluster/flux/secret.yaml` is not applied by hand either. It holds `${...}`
placeholders and arrives through the `flux` tag's own Kustomization, with substitution, once
cluster-vars is up (operator ruling, 2026-10-22).

Run: uv run pytest scripts/renderer/tests/test_renderer_bootstrap_documents.py
"""

import pytest

import bootstrap_drill as drill
from bundle_census import NAMED_MEMBERS
from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from renderer import homelab_render

SYNC = ("cluster", "flux-deferred", "sync.yaml")


def _docs(path):
    return [d for d in yaml_fast.safe_load_all(path.read_text()) if d]


def _sync(out):
    return _docs(out.joinpath(*SYNC))


@pytest.fixture(scope="module")
def flux_render(tmp_path_factory):
    """`homelab-render cluster --role flux` at the head, where `flux` is Ansible-owned."""
    out = tmp_path_factory.mktemp("flux") / "out"
    assert homelab_render.cluster(out, ["flux"], None) == RENDER_OK
    return out


@pytest.fixture(scope="module")
def published_render(tmp_path_factory):
    """Two flux-owned tags with a `dependsOn` chain, plus every plumbing tag already owned."""
    out = tmp_path_factory.mktemp("published") / "out"
    plumbing = set(NAMED_MEMBERS) & homelab_render.flux_owned_tags()
    roles = sorted({"traefik", "traefik-crds"} | plumbing)
    assert homelab_render.cluster(out, roles, None) == RENDER_OK
    return out, plumbing


def test_the_ansible_owned_flux_role_still_renders_the_sync_directory(flux_render):
    assert drill.sync_problems(_sync(flux_render)) == []


def test_a_renderer_that_gates_the_sync_directory_on_the_owner_is_caught():
    assert drill.sync_problems([]) == [
        "flux-deferred holds no OCIRepository homelab",
        "flux-deferred holds no Kustomization homelab",
    ]


def test_the_documents_applied_by_hand_hold_no_placeholder(
    flux_render, published_render
):
    by_hand = [d for d in _sync(flux_render) if d["metadata"]["name"] == "homelab"]
    for path in sorted((published_render[0] / "flux").glob("*.yaml")):
        if path.name != "kustomization.yaml":
            by_hand += _docs(path)
    assert len(by_hand) >= 5
    assert drill.placeholder_problems(by_hand) == []


def test_an_ocirepository_url_holding_a_placeholder_is_caught(flux_render):
    docs = _sync(flux_render)
    docs[0]["spec"]["url"] = "oci://ghcr.io/${owner}/homelab-manifests"
    assert drill.placeholder_problems(docs)


def test_no_plumbing_closure_reaches_flux_deferred_or_the_root(published_render):
    out, plumbing = published_render
    depends = {}
    for path in (out / "flux").glob("*.yaml"):
        for doc in _docs(path):
            if doc["kind"] == "Kustomization" and "spec" in doc:
                names = doc["spec"].get("dependsOn", [])
                depends[doc["metadata"]["name"]] = [d["name"] for d in names]
    # The floor: traefik's chain is non-empty, so a renderer that dropped dependsOn fails here.
    assert drill.dependson_closure(depends, {"traefik"}) >= {
        "traefik-crds",
        "cluster-vars",
    }
    # Every rendered Kustomization, a superset of the plumbing tags. At this head `plumbing` is
    # empty (no plumbing tag is flux-owned yet), so the superset is what the test judges.
    assert drill.closure_problems(depends, set(depends)) == []
    assert plumbing <= set(depends)
