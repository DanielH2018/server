"""The generated `dependsOn` and the reserved tag names (D4.2).

Most fixtures hand `bundle_files` a fixture `Ordering`, so no role renders. The reserved-name
refusal and the cycle refusal run the real CLI on littlelink and bento-pdf. The plan is
docs/target-architecture/deploy/d4-ordering.md, *The renderer writes `dependsOn`*.

Run: uv run pytest scripts/renderer/tests/test_renderer_bundle_ordering.py
"""

import pytest

from lib import yaml_fast
from lib.exit_codes import RENDER_REFUSED
from renderer import homelab_render
from renderer.renderer_lib import cluster as cluster_mod
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE, SHARED_KINDS, bundle_files
from renderer.renderer_lib.ordering import (
    ANSIBLE_OWNED_DEPS,
    Ordering,
    dependency_cycle,
    kustomization_edges,
)

TIMEOUT = "600s"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
CONFIGMAP = "kind: ConfigMap\nmetadata:\n  name: x\n  namespace: {namespace}\n"


def _docs(ordering: Ordering, tags, directories=None) -> dict[str, dict]:
    """Every tag Kustomization `bundle_files` writes for `tags`, by name."""
    files = bundle_files(directories or {}, {}, tags, TIMEOUT, DOTENV, (), ordering)
    return {
        doc["metadata"]["name"]: doc
        for tag in tags
        for doc in yaml_fast.safe_load_all(files[f"flux/{tag}.yaml"])
    }


def _depends(doc: dict) -> list[str]:
    return [edge["name"] for edge in doc["spec"].get("dependsOn", [])]


def _dropped(doc: dict) -> str | None:
    return (doc["metadata"].get("annotations") or {}).get(ANSIBLE_OWNED_DEPS)


def _deferring(*tags: str, namespace: str = "homelab") -> dict:
    """A `<tag>` and a `<tag>-deferred` directory for each of `tags`."""
    files = {"x.yaml": CONFIGMAP.format(namespace=namespace)}
    return {name: files for tag in tags for name in (tag, f"{tag}-deferred")}


def test_an_edge_to_an_ansible_owned_role_is_dropped_and_annotated():
    ordering = Ordering(deps={"texbrain": ["authelia", "traefik"]})
    doc = _docs(ordering, ["texbrain"])["texbrain"]
    assert "dependsOn" not in doc["spec"]
    assert _dropped(doc) == "authelia,traefik"


def test_an_edge_between_two_flux_owned_roles_is_kept():
    ordering = Ordering(deps={"texbrain": ["bento-pdf", "traefik"], "bento-pdf": []})
    docs = _docs(ordering, ["bento-pdf", "texbrain"])
    assert _depends(docs["texbrain"]) == ["bento-pdf"]
    assert _dropped(docs["texbrain"]) == "traefik"
    assert "annotations" not in docs["bento-pdf"]["metadata"]


def test_a_flux_owned_role_outside_the_render_is_kept_not_annotated():
    """Ownership comes from the inventory, so a `--role` subset does not move an edge."""
    ordering = Ordering(
        deps={"texbrain": ["bento-pdf"]},
        flux_owned=frozenset({"texbrain", "bento-pdf"}),
    )
    doc = _docs(ordering, ["texbrain"])["texbrain"]
    assert _depends(doc) == ["bento-pdf"] and _dropped(doc) is None


def test_an_applies_last_tag_depends_on_other_deferred_kustomizations_not_its_own():
    ordering = Ordering(
        deps={"last": ["other", "plain"]}, applies_last=frozenset({"last"})
    )
    directories = _deferring("last", "other") | {"plain": {}}
    docs = _docs(ordering, ["last", "other", "plain"], directories)
    assert _depends(docs["last"]) == ["other", "other-deferred", "plain"]
    assert _depends(docs["last-deferred"]) == ["last"]
    assert _depends(docs["other"]) == []


def test_a_deferred_document_in_a_role_created_namespace_waits_on_that_role():
    """flux-owned, the namespace's role is an edge; Ansible-owned, it is annotated."""
    directories = _deferring("headlamp", namespace="observability")
    roles = {"observability": "observability"}
    owned = Ordering(
        namespace_roles=roles, flux_owned=frozenset({"headlamp", "observability"})
    )
    doc = _docs(owned, ["headlamp"], directories)["headlamp-deferred"]
    assert _depends(doc) == ["headlamp", "observability"]
    ansible = Ordering(namespace_roles=roles)
    doc = _docs(ansible, ["headlamp"], directories)["headlamp-deferred"]
    assert _depends(doc) == ["headlamp"]
    assert _dropped(doc) == "observability"


def test_a_cycle_in_the_map_fails_the_bundle():
    ordering = Ordering(deps={"a": ["b"], "b": ["a"]})
    with pytest.raises(ValueError, match=r"cycle, a -> b -> a"):
        _docs(ordering, ["a", "b"])


def test_the_generated_edges_beside_a_soak_shaped_kustomization_are_clean():
    """`soak` depends on every Kustomization, as D5's will. The writer adds no edge back."""
    ordering = Ordering(deps={"last": ["other"]}, applies_last=frozenset({"last"}))
    edges = kustomization_edges(
        ["last", "other"], _deferring("last", "other"), ordering
    )
    graph = {name: set(edge.kept) for name, edge in edges.items()}
    graph["soak"] = set(edges)
    assert dependency_cycle(graph) == []
    assert dependency_cycle({**graph, "last": graph["last"] | {"soak"}})
    assert dependency_cycle({**graph, "last": graph["last"] | {"last-deferred"}}) == [
        "last",
        "last-deferred",
        "last",
    ]


def test_the_render_refuses_a_cycle_and_writes_nothing(tmp_path, capsys):
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    out = tmp_path / "out"
    tags = ["bento-pdf", "littlelink"]
    code = homelab_render.cluster(
        out,
        tags,
        None,
        flux_owned=lambda: set(tags),
        cluster_file=cluster_file,
        ordering=lambda owned: Ordering(
            deps={"bento-pdf": ["littlelink"], "littlelink": ["bento-pdf"]}
        ),
    )
    assert code == RENDER_REFUSED
    assert "cycle, bento-pdf -> littlelink -> bento-pdf" in capsys.readouterr().err
    assert not out.exists() or list(out.iterdir()) == []


def test_the_live_ordering_reads_the_play_map_and_its_rules():
    """bento-pdf is routed behind Authelia, so the play's map gives it both edges."""
    ordering = cluster_mod.bundle_ordering({"bento-pdf"})
    assert {"authelia", "traefik"} <= set(ordering.deps["bento-pdf"])
    assert "netpol-baseline" in ordering.applies_last
    assert ordering.namespace_roles == {"observability": "observability"}


# -- the reserved names ----------------------------------------------------------------------


def test_a_tag_named_homelab_is_flagged():
    with pytest.raises(cluster_mod.RenderRefused, match=r"\['homelab'\]"):
        cluster_mod.select_tags(["homelab"])


def test_a_tag_named_after_a_shared_kind_is_flagged():
    with pytest.raises(cluster_mod.RenderRefused, match=r"\['littlelink'\]"):
        cluster_mod.select_tags(["littlelink"], kinds=frozenset({"littlelink"}))


def test_a_tag_named_cluster_vars_is_flagged():
    with pytest.raises(cluster_mod.RenderRefused, match=r"\['cluster-vars'\]"):
        cluster_mod.select_tags(["cluster-vars"])


def test_an_ordinary_tag_is_clean():
    assert cluster_mod.select_tags(["littlelink"]) == ["littlelink"]


@pytest.mark.parametrize(
    "tag, kinds",
    [
        ("homelab", SHARED_KINDS),
        ("littlelink", frozenset({"littlelink"})),
        ("fixture-deferred", SHARED_KINDS),
    ],
)
def test_the_render_refuses_a_reserved_tag_and_writes_nothing(
    tmp_path, capsys, tag, kinds
):
    out = tmp_path / "out"
    out.mkdir()
    assert homelab_render.cluster(out, [tag], None, kinds=kinds) == RENDER_REFUSED
    assert "the root homelab, a SHARED_KINDS member, or a name ending in -deferred" in (
        capsys.readouterr().err
    )
    assert list(out.iterdir()) == []
