"""The generated `dependsOn`, the per-tag kinds and the reserved tag names (D4.2, D5.1a1).

Most fixtures hand `bundle_files` a fixture `Ordering`, so no role renders. The reserved-name
refusal and the cycle refusal run the real CLI on littlelink and bento-pdf. The plans are
docs/target-architecture/deploy/d4-ordering.md, *The renderer writes `dependsOn`*, and
d5-bulk-move.md, *Prune exemptions, digests and the collision census*.

Run: uv run pytest scripts/renderer/tests/test_renderer_bundle_ordering.py
"""

import pytest
import yaml

from lib import yaml_fast
from lib.exit_codes import RENDER_REFUSED
from renderer import homelab_render
from renderer.renderer_lib import cluster as cluster_mod
from renderer.renderer_lib.bundle import (
    CLUSTER_VARS_FILE,
    SHARED_KINDS,
    bundle_files,
    digests,
)
from renderer.renderer_lib.ordering import (
    ANSIBLE_OWNED_DEPS,
    Ordering,
    dependency_cycle,
    kustomization_edges,
    verify_after,
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
    assert _dropped(docs["bento-pdf"]) is None


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
    "tag", ["soak", "homelab-namespace", "fixture-snapshot", "fixture-verify"]
)
def test_a_tag_taking_a_name_d5_reserves_is_flagged(tag):
    """`soak` and `homelab-namespace` are refused as SHARED_KINDS members, and the suffixes alone."""
    assert {"homelab-namespace", "soak"} <= SHARED_KINDS
    # The reserved-name text, because none of these is a render target and the
    # unknown-target refusal also names the tag.
    with pytest.raises(cluster_mod.RenderRefused) as refused:
        cluster_mod.select_tags([tag])
    assert str(refused.value).startswith(f"['{tag}'] take a name the bundle reserves")


@pytest.mark.parametrize(
    "tag, kinds",
    [
        ("homelab", SHARED_KINDS),
        ("littlelink", frozenset({"littlelink"})),
        ("fixture-deferred", SHARED_KINDS),
        ("soak", SHARED_KINDS),
    ],
)
def test_the_render_refuses_a_reserved_tag_and_writes_nothing(
    tmp_path, capsys, tag, kinds
):
    out = tmp_path / "out"
    out.mkdir()
    assert homelab_render.cluster(out, [tag], None, kinds=kinds) == RENDER_REFUSED
    assert (
        "a name ending in -deferred, -snapshot, -prepull, -precheck or -verify"
        in capsys.readouterr().err
    )
    assert list(out.iterdir()) == []


# -- the per-tag kinds (D5.1a1) --------------------------------------------------------------


def _every_kind() -> dict:
    """Directories for `pihole` with every per-tag kind, and a plain `bento-pdf`."""
    files = {"x.yaml": CONFIGMAP.format(namespace="homelab")}
    names = [
        "pihole",
        "pihole-deferred",
        "pihole-snapshot",
        "pihole-verify",
        "bento-pdf",
    ]
    return {name: files for name in names}


def test_a_tags_kinds_follow_it_in_its_file_in_suffix_order():
    docs = _docs(Ordering(), ["bento-pdf", "pihole"], _every_kind())
    files = bundle_files(_every_kind(), {}, ["bento-pdf", "pihole"], TIMEOUT, DOTENV)
    assert [
        d["metadata"]["name"]
        for d in yaml_fast.safe_load_all(files["flux/pihole.yaml"])
    ] == ["pihole", "pihole-deferred", "pihole-snapshot", "pihole-verify"]
    assert docs["pihole-snapshot"]["spec"]["path"] == "./cluster/pihole-snapshot"


def _orphan_missing(files: dict[str, str]) -> list[str]:
    """Each Kustomization in the `flux/` files that lacks `deletionPolicy: Orphan`."""
    return [
        doc["metadata"]["name"]
        for path, text in files.items()
        if path.startswith("flux/") and not path.endswith("kustomization.yaml")
        for doc in yaml_fast.safe_load_all(text)
        if doc["spec"].get("deletionPolicy") != "Orphan"
    ]


def test_every_kustomization_of_every_kind_sets_deletion_policy_orphan():
    files = bundle_files(_every_kind(), {}, ["bento-pdf", "pihole"], TIMEOUT, DOTENV)
    flux = [p for p in files if p.startswith("flux/")]
    assert {"flux/pihole.yaml", "flux/bento-pdf.yaml", "flux/cluster-vars.yaml"} <= set(
        flux
    )
    assert _orphan_missing(files) == []


def test_a_kustomization_without_deletion_policy_orphan_is_flagged():
    files = bundle_files(_every_kind(), {}, ["bento-pdf", "pihole"], TIMEOUT, DOTENV)
    files["flux/pihole.yaml"] = files["flux/pihole.yaml"].replace(
        "deletionPolicy: Orphan", "deletionPolicy: Delete", 3
    )
    assert _orphan_missing(files) == ["pihole", "pihole-deferred", "pihole-snapshot"]


def test_a_verify_kind_depends_on_its_tags_deferred_kustomization():
    docs = _docs(Ordering(), ["pihole"], _every_kind())
    assert _depends(docs["pihole-verify"]) == ["pihole-deferred"]


def test_a_verify_kind_of_a_tag_with_no_deferred_files_depends_on_the_tag():
    directories = {name: {} for name in ("bento-pdf", "bento-pdf-verify")}
    docs = _docs(Ordering(), ["bento-pdf"], directories)
    assert _depends(docs["bento-pdf-verify"]) == ["bento-pdf"]


def test_a_verify_kind_waits_on_the_last_kustomization_of_each_tag_it_names():
    """A named tag's `-deferred` stands in for it, and an Ansible-owned one is annotated."""
    ordering = Ordering(
        flux_owned=frozenset({"bento-pdf", "pihole"}),
        verify_after={"bento-pdf": ["pihole", "traefik"]},
    )
    directories = _every_kind() | {"bento-pdf-verify": {}}
    doc = _docs(ordering, ["bento-pdf", "pihole"], directories)["bento-pdf-verify"]
    assert _depends(doc) == ["bento-pdf", "pihole-deferred"]
    assert _dropped(doc) == "traefik"


def test_crowdsec_verify_waits_on_the_rolled_traefik():
    """The ban proof reads the edge, so it must probe the Traefik pod a roll brought (#5999)."""
    ordering = cluster_mod.bundle_ordering({"crowdsec", "traefik"})
    assert ordering.verify_after["crowdsec"] == ["traefik"]
    directories = {name: {} for name in ("crowdsec", "crowdsec-verify", "traefik")}
    docs = _docs(ordering, ["crowdsec", "traefik"], directories)
    assert "traefik" in _depends(docs["crowdsec-verify"])


@pytest.mark.parametrize("value", ["traefik", [], ["crowdsec"], "{{ edges }}"])
def test_a_verify_after_that_is_not_a_list_of_other_tags_is_a_problem(tmp_path, value):
    defaults = tmp_path / "crowdsec" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text(yaml.safe_dump({"flux_verify_after": value}))
    found, problems = verify_after(["crowdsec"], tmp_path)
    assert found == {}
    assert problems and "flux_verify_after" in problems[0]


def test_a_snapshot_kind_inherits_its_tags_edges_and_the_tag_waits_on_it():
    ordering = Ordering(deps={"pihole": ["bento-pdf", "traefik"]})
    docs = _docs(ordering, ["bento-pdf", "pihole"], _every_kind())
    assert _depends(docs["pihole-snapshot"]) == ["bento-pdf"]
    assert _dropped(docs["pihole-snapshot"]) == "traefik"
    assert _depends(docs["pihole"]) == ["bento-pdf", "pihole-snapshot"]


def test_a_prepull_kind_inherits_its_tags_edges_beside_the_snapshot():
    ordering = Ordering(deps={"pihole": ["bento-pdf", "traefik"]})
    directories = _every_kind() | {"pihole-prepull": {}}
    docs = _docs(ordering, ["bento-pdf", "pihole"], directories)
    assert _depends(docs["pihole-prepull"]) == ["bento-pdf"]
    assert _dropped(docs["pihole-prepull"]) == "traefik"
    assert _depends(docs["pihole-snapshot"]) == ["bento-pdf"]
    assert _depends(docs["pihole"]) == [
        "bento-pdf",
        "pihole-prepull",
        "pihole-snapshot",
    ]


def test_an_applies_last_tag_waits_on_no_other_tags_snapshot_or_verify():
    """A guard of the `# DECIDED:` choice in ordering.py, not of a change this step made.

    It fails only on a change that adds such an edge on purpose.
    """
    ordering = Ordering(deps={"last": []}, applies_last=frozenset({"last"}))
    docs = _docs(ordering, ["last", "pihole"], _every_kind() | {"last": {}})
    assert _depends(docs["last"]) == ["pihole-deferred"]


def test_a_tags_snapshot_and_verify_directories_move_its_footprint_not_its_own():
    before = {"cluster/pihole/x.yaml": b"a", "cluster/pihole-verify/x.yaml": b"a"}
    after = before | {
        "cluster/pihole-verify/x.yaml": b"b",
        "cluster/pihole-snapshot/x.yaml": b"b",
    }
    old, new = digests(before, ["pihole"]), digests(after, ["pihole"])
    assert old["pihole"]["own"] == new["pihole"]["own"]
    assert old["pihole"]["footprint"] != new["pihole"]["footprint"]
    assert new["_unclassified"]["paths"] == []
