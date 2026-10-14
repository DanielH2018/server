"""The bundle writer: `flux/<tag>.yaml`, the root's list, the reserved names and `digests.json`.

Most fixtures are file maps handed to `bundle_files` and `digests`, so no role renders. Two
tests run the real CLI on littlelink: the determinism pair and the reserved-name refusal.

Run: uv run pytest scripts/renderer/tests/test_renderer_bundle.py
"""

import json
import pytest

from changed_set import changed_set
from lib import yaml_fast
from lib.exit_codes import RENDER_REFUSED
from renderer import homelab_render
from renderer.renderer_lib import cluster as cluster_mod
from renderer.renderer_lib.bundle import (
    DIGESTS,
    FLUX_INDEX,
    SHARED_KINDS,
    UNCLASSIFIED,
    bundle_files,
    digests,
    rules_missing,
)
from renderer.renderer_lib.cluster import KUSTOMIZATION, RenderedTag, kustomization_text
from renderer.renderer_lib.placeholders import apply_placeholders, sentinel

TIMEOUT = "600s"
DEPLOYMENT = "kind: Deployment\nmetadata:\n  name: {name}\nspec:\n  value: {value}\n"


def _tag(name: str, value: str = "plain") -> RenderedTag:
    files = {"deployment.yaml": DEPLOYMENT.format(name=name, value=value)}
    return RenderedTag(name, {**files, KUSTOMIZATION: kustomization_text(files)})


def _bundle(*tags: RenderedTag, keys=("email",)) -> dict[str, str]:
    """The `flux/` files and `digests.json` for `tags`, through the placeholder passes."""
    result = apply_placeholders(list(tags), keys)
    directories = {n: f for t in result.tags for n, f in t.directories().items()}
    return bundle_files(
        directories, result.key_sets, [t.tag for t in result.tags], TIMEOUT
    )


def _expected(name: str) -> dict:
    """D2's *The per-tag Kustomization*, without `postBuild`."""
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": name, "namespace": "flux-system"},
        "spec": {
            "sourceRef": {"kind": "OCIRepository", "name": "homelab"},
            "path": f"./cluster/{name}",
            "interval": "10m",
            "retryInterval": "2m",
            "timeout": TIMEOUT,
            "prune": True,
            "wait": True,
            "deletionPolicy": "Orphan",
        },
    }


def test_a_tag_holding_a_placeholder_gets_post_build_naming_the_cluster_vars_secret():
    files = _bundle(_tag("bento-pdf", sentinel("email")))
    expected = _expected("bento-pdf")
    expected["spec"]["postBuild"] = {
        "substituteFrom": [{"kind": "Secret", "name": "homelab-cluster-vars"}]
    }
    assert yaml_fast.safe_load(files["flux/bento-pdf.yaml"]) == expected


def test_a_tag_holding_no_placeholder_gets_no_post_build():
    files = _bundle(_tag("bento-pdf", "${HOME}"))
    assert yaml_fast.safe_load(files["flux/bento-pdf.yaml"]) == _expected("bento-pdf")


def test_the_root_lists_every_file_in_flux():
    files = _bundle(_tag("bento-pdf"), _tag("littlelink", sentinel("email")))
    in_flux = sorted(
        p.removeprefix("flux/")
        for p in files
        if p.startswith("flux/") and p != FLUX_INDEX
    )
    assert in_flux == ["bento-pdf.yaml", "littlelink.yaml"]
    assert yaml_fast.safe_load(files[FLUX_INDEX])["resources"] == in_flux


def test_a_published_tag_with_a_deferred_directory_is_flagged():
    deferred = RenderedTag(
        "pihole", _tag("pihole").files, {"x.yaml": "kind: ConfigMap\n"}
    )
    with pytest.raises(ValueError, match=r"D4\.2"):
        _bundle(deferred)


def test_a_tag_named_homelab_is_flagged():
    with pytest.raises(cluster_mod.RenderRefused, match=r"\['homelab'\]"):
        cluster_mod.select_tags(["homelab"])


def test_a_tag_named_after_a_shared_kind_is_flagged():
    with pytest.raises(cluster_mod.RenderRefused, match=r"\['littlelink'\]"):
        cluster_mod.select_tags(["littlelink"], kinds=frozenset({"littlelink"}))


def test_an_ordinary_tag_is_clean():
    assert cluster_mod.select_tags(["littlelink"]) == ["littlelink"]


@pytest.mark.parametrize(
    "tag, kinds", [("homelab", SHARED_KINDS), ("littlelink", frozenset({"littlelink"}))]
)
def test_the_render_refuses_a_reserved_tag_and_writes_nothing(
    tmp_path, capsys, tag, kinds
):
    out = tmp_path / "out"
    out.mkdir()
    assert homelab_render.cluster(out, [tag], None, kinds=kinds) == RENDER_REFUSED
    assert "the root homelab or a SHARED_KINDS member" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_two_renders_of_one_tree_write_identical_bundles(tmp_path):
    texts = []
    for name in ("first", "second"):
        out = tmp_path / name
        assert (
            homelab_render.main(["cluster", "--out", str(out), "--role", "littlelink"])
            == 0
        )
        texts.append(
            {
                p.relative_to(out).as_posix(): p.read_bytes()
                for p in sorted(out.rglob("*"))
                if p.is_file() and not p.is_relative_to(out / "cluster")
            }
        )
    assert texts[0] == texts[1]
    assert sorted(texts[0]) == [
        DIGESTS,
        "flux/kustomization.yaml",
        "flux/littlelink.yaml",
    ]
    assert set(json.loads(texts[0][DIGESTS])) == {"littlelink", UNCLASSIFIED}


BASE = {
    "cluster/bento-pdf/deployment.yaml": b"a",
    "cluster/bento-pdf-deferred/x.yaml": b"d",
    "flux/bento-pdf.yaml": b"k",
    "cluster/sonarr/deployment.yaml": b"s",
    "flux/sonarr.yaml": b"ks",
    FLUX_INDEX: b"index",
}
TAGS = ("bento-pdf", "sonarr")


def _moved(edited: dict, **kwargs) -> dict[str, set[str]]:
    """Which of each name's two digests moved between BASE and `edited`."""
    old, new = digests(BASE, TAGS, **kwargs), digests(edited, TAGS, **kwargs)
    return {
        name: {k for k in ("own", "footprint") if old[name][k] != new[name][k]}
        for name in new
    }


def test_an_edit_under_a_tags_directory_moves_its_own_and_its_footprint():
    moved = _moved({**BASE, "cluster/bento-pdf/deployment.yaml": b"b"})
    assert moved == {
        "bento-pdf": {"own", "footprint"},
        "sonarr": set(),
        UNCLASSIFIED: set(),
    }


def test_an_edit_to_its_kustomization_or_deferred_directory_moves_only_its_footprint():
    for path in ("flux/bento-pdf.yaml", "cluster/bento-pdf-deferred/x.yaml"):
        moved = _moved({**BASE, path: b"changed"})
        assert moved["bento-pdf"] == {"footprint"}, path
        assert moved["sonarr"] == set(), path


def test_the_index_paths_move_no_digest():
    moved = _moved(
        {**BASE, FLUX_INDEX: b"other", DIGESTS: b"{}", "images/t.json": b"1"}
    )
    assert all(not keys for keys in moved.values())


def test_a_path_that_counts_for_no_name_lands_under_unclassified():
    stray = {**BASE, "cluster/gone/deployment.yaml": b"x", "README": b"r"}
    result = digests(stray, TAGS)
    assert result[UNCLASSIFIED]["paths"] == ["README", "cluster/gone/deployment.yaml"]
    assert digests(BASE, TAGS)[UNCLASSIFIED]["paths"] == []
    assert _moved(stray)[UNCLASSIFIED] == {"own", "footprint"}


def test_a_shared_kinds_file_moves_the_footprint_of_each_tag_its_rule_lists():
    kinds = {
        "kinds": {"cluster-vars"},
        "rules": {"cluster-vars": lambda f: f.substituting},
    }
    edited = {**BASE, "cluster/cluster-vars/secret.yaml": b"v2"}
    base = {**BASE, "cluster/cluster-vars/secret.yaml": b"v1"}
    old = digests(base, TAGS, substituting={"bento-pdf"}, **kinds)
    new = digests(edited, TAGS, substituting={"bento-pdf"}, **kinds)
    assert old["bento-pdf"]["footprint"] != new["bento-pdf"]["footprint"]
    assert old["bento-pdf"]["own"] == new["bento-pdf"]["own"]
    assert old["sonarr"] == new["sonarr"]
    assert old["cluster-vars"]["own"] != new["cluster-vars"]["own"]
    assert old[UNCLASSIFIED] == new[UNCLASSIFIED]


def test_a_shared_kind_without_a_rule_lands_under_unclassified():
    files = {**BASE, "cluster/cluster-vars/secret.yaml": b"v1"}
    result = digests(files, TAGS, kinds={"cluster-vars"}, rules={})
    assert result[UNCLASSIFIED]["paths"] == ["cluster/cluster-vars/secret.yaml"]


def test_every_shared_kind_has_a_footprint_rule():
    assert rules_missing(SHARED_KINDS) == []
    assert rules_missing({"cluster-vars"}, {"cluster-vars": lambda f: f.tags}) == []


def test_a_shared_kind_without_a_footprint_rule_is_flagged():
    assert rules_missing({"cluster-vars"}, {}) == ["cluster-vars"]


def test_the_written_digests_json_is_the_shape_land_wait_reads():
    """D2.3's `changed_set` reads the file the renderer writes, without widening."""
    old = json.loads(_bundle(_tag("bento-pdf"), _tag("sonarr"))[DIGESTS])
    new = json.loads(_bundle(_tag("bento-pdf", "edited"), _tag("sonarr"))[DIGESTS])
    graph = {"bento-pdf": [], "sonarr": []}
    assert changed_set(old, old, graph).names == frozenset()
    assert changed_set(old, old, graph).widened == ""
    moved = changed_set(new, old, graph)
    assert moved.names == {"bento-pdf"} and moved.widened == ""
