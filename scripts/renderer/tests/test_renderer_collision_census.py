"""D5.1a2's collision census: no two deploy tags render the same object.

An object two tags render has two writers once either moves to Flux, one per plane, and each
apply reverts the other's. The census keys every rendered document on its apiVersion, kind,
namespace and name, and fails on a key two tags share unless `SHARED_OBJECTS` lists it with
its reason. A listed pair moves to Flux in one PR. A tag's `-deferred`, `-snapshot` and
`-verify` directories count as the tag's own.

The live test renders every render target in placeholder mode, as the render job does.
The plan is docs/target-architecture/deploy/d5-bulk-move.md, *Prune exemptions, digests and
the collision census (D5.1a1, D5.1a2)*.

Run: uv run pytest scripts/renderer/tests/test_renderer_collision_census.py
"""

from collections import defaultdict
from collections.abc import Mapping

from deploy_tools.render_targets import render_targets
from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from lib.exit_codes import RENDER_OK
from lib.repo_paths import HOST_VARS
from render_share import placeholder_renderer
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE, KUSTOMIZATION
from renderer.renderer_lib.ordering import split_kind

Key = tuple[str, str, str, str]

# Each object two tags may both render, mapped to the reason. Empty: no two of the render
# targets share one.
SHARED_OBJECTS: dict[Key, str] = {}
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
MIDDLEWARE = (
    "apiVersion: traefik.io/v1alpha1\nkind: Middleware\nmetadata:\n"
    "  name: {name}\n  namespace: {namespace}\nspec:\n  headers: {{}}\n"
)


def owners(directories: Mapping[str, Mapping[str, str]]) -> dict[Key, set[str]]:
    """Each object rendered under `directories` mapped to the deploy tags that render it."""
    found: dict[Key, set[str]] = defaultdict(set)
    for directory, files in directories.items():
        tag = split_kind(directory)[0]
        for name, text in files.items():
            if name == KUSTOMIZATION:
                continue
            for doc in yaml_fast.safe_load_all(text):
                if not isinstance(doc, dict):
                    continue
                meta = doc.get("metadata") or {}
                key = (
                    str(doc.get("apiVersion")),
                    str(doc.get("kind")),
                    str(meta.get("namespace") or ""),
                    str(meta.get("name")),
                )
                found[key].add(tag)
    return found


def collisions(
    found: Mapping[Key, set[str]], shared: Mapping[Key, str] = SHARED_OBJECTS
) -> dict[Key, list[str]]:
    """Each object more than one tag renders and `shared` does not list, with those tags."""
    return {
        key: sorted(tags)
        for key, tags in sorted(found.items())
        if len(tags) > 1 and key not in shared
    }


def _middleware(name: str = "headers", namespace: str = "homelab") -> str:
    return MIDDLEWARE.format(name=name, namespace=namespace)


def test_two_tags_rendering_one_middleware_are_flagged():
    found = owners(
        {"one": {"mw.yaml": _middleware()}, "two-deferred": {"x.yaml": _middleware()}}
    )
    key = ("traefik.io/v1alpha1", "Middleware", "homelab", "headers")
    assert collisions(found) == {key: ["one", "two"]}


def test_one_name_in_two_namespaces_or_one_tags_two_directories_is_clean():
    found = owners(
        {
            "one": {"mw.yaml": _middleware()},
            "one-deferred": {"mw.yaml": _middleware()},
            "two": {"mw.yaml": _middleware(namespace="other")},
        }
    )
    assert collisions(found) == {}


def test_a_listed_pair_is_clean():
    found = owners({"one": {"a.yaml": _middleware()}, "two": {"b.yaml": _middleware()}})
    key = ("traefik.io/v1alpha1", "Middleware", "homelab", "headers")
    assert collisions(found, {key: "fixture: both move in one PR"}) == {}


def test_no_two_render_targets_render_one_object(tmp_path):
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out, None, None, render=placeholder_renderer(), cluster_file=cluster_file
    )
    assert code == RENDER_OK
    directories = {
        d.name: {f.name: f.read_text() for f in d.iterdir()}
        for d in (out / "cluster").iterdir()
    }
    found = owners(directories)
    # A walk that reached nothing would pass, so the render must hold every target and
    # objects from two named roles.
    assert set(render_targets(K8S_HOST, HOST_VARS)) <= {
        split_kind(d)[0] for d in directories
    }
    assert ("v1", "Namespace", "", "observability") in found
    assert any(k[1] == "Middleware" for k in found)
    assert collisions(found) == {}
    # A listed pair that no longer collides is a stale entry.
    stale = [k for k in SHARED_OBJECTS if len(found.get(k, ())) < 2]
    assert stale == []
    assert all(reason.strip() for reason in SHARED_OBJECTS.values())
