"""The documents the bootstrap procedure applies by hand (D5 steps 28 and 29, R4 PR 7b).

Scope of "applied by hand": the OCIRepository and the root from `cluster/flux-deferred/` (steps 5
and 9) and every `flux/<tag>.yaml` Kustomization (step 6). Step 3 applies `components.yaml`
alone, and the test does not scan it: `components.yaml` quotes `${var:=default}` in a CRD
description. `cluster/flux/secret.yaml` is not applied by hand either. It holds `${...}`
placeholders and arrives through the `flux` tag's own Kustomization, with substitution, once
cluster-vars is up (operator ruling, 2026-10-22).

From D5 step 29 the flux role is flux-owned, so its render also writes `flux/flux.yaml`: the
Kustomization `flux` at `prune: false` and `flux-deferred` at `prune: true`. The sync objects
`flux-deferred` applies keep D1's source, so a change to it fails here rather than cutting
Flux off from its artifact.

Run: uv run pytest scripts/renderer/tests/test_renderer_bootstrap_documents.py
"""

import copy

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
    """`homelab-render cluster --role flux` at the head, where `flux` is flux-owned."""
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


def test_the_flux_role_renders_the_sync_directory(flux_render):
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
    # Every rendered Kustomization, a superset of the plumbing tags that are flux-owned.
    assert drill.closure_problems(depends, set(depends)) == []
    assert plumbing <= set(depends)


def test_flux_renders_prune_false_and_flux_deferred_prune_true(flux_render):
    """Flux's own CRDs are never pruned, and `flux-deferred`'s prune deletes `verify-drill`."""
    docs = _docs(flux_render / "flux" / "flux.yaml")
    assert [(d["metadata"]["name"], d["spec"]["prune"]) for d in docs] == [
        ("flux", False),
        ("flux-deferred", True),
    ]


# D1's values, written out rather than read from the role's defaults or from `forge.py`, so a
# change to either fails here instead of moving the pin with it.
SOURCE_URL = "oci://ghcr.io/danielh2018/homelab-manifests"
VERIFY = {
    "provider": "cosign",
    "matchOIDCIdentity": [
        {
            "issuer": r"^https://token\.actions\.githubusercontent\.com$",
            "subject": (
                r"^https://github\.com/DanielH2018/server/\.github/workflows/"
                r"promote\.yml@refs/heads/master$"
            ),
        }
    ],
}
PINNED = {"homelab": {"tag": "prod"}, "last-good": {"tag": "last-good"}}


def sync_pin_problems(docs: list[dict]) -> list[str]:
    """Each way the sync OCIRepositories differ from D1's URL, `ref` and `verify` block."""
    repos = {
        d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "OCIRepository"
    }
    problems = [f"no OCIRepository {name}" for name in sorted(set(PINNED) - set(repos))]
    for name, ref in sorted(PINNED.items()):
        spec = repos.get(name)
        if spec is None:
            continue
        for field, want in (("url", SOURCE_URL), ("ref", ref), ("verify", VERIFY)):
            if spec.get(field) != want:
                problems.append(f"{name}.{field} is {spec.get(field)!r}, not {want!r}")
    return problems


def test_sync_pin_problems_is_clean_on_the_render(flux_render):
    assert sync_pin_problems(_sync(flux_render)) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "oci://ghcr.io/someone-else/homelab-manifests"),
        ("ref", {"tag": "latest"}),
        ("verify", None),
    ],
    ids=["url", "ref", "verify"],
)
def test_sync_pin_problems_is_flagged_on_a_moved_source(flux_render, field, value):
    docs = copy.deepcopy(_sync(flux_render))
    repo = next(
        d
        for d in docs
        if d["metadata"]["name"] == "homelab" and d["kind"] == "OCIRepository"
    )
    repo["spec"][field] = value
    [problem] = sync_pin_problems(docs)
    assert problem.startswith(f"homelab.{field} is ")
