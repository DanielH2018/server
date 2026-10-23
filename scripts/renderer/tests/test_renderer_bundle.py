"""The bundle writer: `flux/<tag>.yaml`, the root's list and `digests.json`.

Most fixtures are file maps handed to `bundle_files` and `digests`, so no role renders. The
determinism pair runs the real CLI on littlelink. `dependsOn` and the reserved names are in
`test_renderer_bundle_ordering.py`. Every render that publishes copies `DOTENV`, a fixture
cluster file in `tmp_path` holding fake ciphertext, never the tree's cluster file.

Run: uv run pytest scripts/renderer/tests/test_renderer_bundle.py
"""

import json
import pytest

from changed_set import changed_set
from lib import yaml_fast
from lib.exit_codes import RENDER_BAD_ARGS, RENDER_OK, RENDER_REFUSED
from renderer import homelab_render
from renderer.renderer_lib.bundle import (
    DIGESTS,
    FLUX_INDEX,
    SHARED_KINDS,
    CLUSTER_VARS_FILE,
    CONTENT_DIGEST,
    UNCLASSIFIED,
    bundle_files,
    digests,
    rules_missing,
)
from renderer.renderer_lib.cluster import KUSTOMIZATION, RenderedTag, kustomization_text
from renderer.renderer_lib.placeholders import apply_placeholders, sentinel

TIMEOUT = "600s"
DEPLOYMENT = "kind: Deployment\nmetadata:\n  name: {name}\nspec:\n  value: {value}\n"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
CLUSTER_VARS_DIR = "cluster/cluster-vars"


@pytest.fixture
def cluster_file(tmp_path):
    """A fixture cluster file holding `DOTENV`."""
    path = tmp_path / CLUSTER_VARS_FILE
    path.write_text(DOTENV)
    return path


def _tag(name: str, value: str = "plain") -> RenderedTag:
    files = {"deployment.yaml": DEPLOYMENT.format(name=name, value=value)}
    return RenderedTag(name, {**files, KUSTOMIZATION: kustomization_text(files)})


def _bundle(*tags: RenderedTag, keys=("email",), dotenv=DOTENV) -> dict[str, str]:
    """The bundle files for `tags`, through the placeholder passes."""
    result = apply_placeholders(list(tags), keys)
    directories = {n: f for t in result.tags for n, f in t.directories().items()}
    return bundle_files(
        directories, result.key_sets, [t.tag for t in result.tags], TIMEOUT, dotenv
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


def _with_content(expected: dict, files: dict[str, str]) -> dict:
    """`expected` carrying the tag's `own` digest from the bundle's `digests.json`."""
    own = json.loads(files[DIGESTS])[expected["metadata"]["name"]]["own"]
    expected["metadata"]["annotations"] = {CONTENT_DIGEST: own}
    return expected


def test_a_tag_holding_a_placeholder_gets_substitute_from_and_the_cluster_vars_edge():
    files = _bundle(_tag("bento-pdf", sentinel("email")))
    expected = _with_content(_expected("bento-pdf"), files)
    expected["spec"]["postBuild"] = {
        "substituteFrom": [{"kind": "Secret", "name": "homelab-cluster-vars"}]
    }
    expected["spec"]["dependsOn"] = [{"name": "cluster-vars"}]
    assert yaml_fast.safe_load(files["flux/bento-pdf.yaml"]) == expected


def test_a_tag_holding_no_placeholder_gets_neither_and_keeps_a_literal_dollar_brace():
    """The plan's negative half, a guard on kept behaviour.

    It goes red on a writer that gives either field to every tag, not on the base commit.
    """
    tag = _tag("bento-pdf", "${HOME}")
    files = _bundle(tag)
    assert yaml_fast.safe_load(files["flux/bento-pdf.yaml"]) == _with_content(
        _expected("bento-pdf"), files
    )
    rendered = apply_placeholders([tag], ("email",)).tags[0].files["deployment.yaml"]
    assert "value: ${HOME}\n" in rendered, (
        "Flux never substitutes it, so it stays unescaped"
    )


def _pihole(main: str, deferred: str) -> RenderedTag:
    return RenderedTag(
        "pihole", _tag("pihole", main).files, _tag("pihole-deferred", deferred).files
    )


def _specs(tag: RenderedTag) -> list[dict]:
    """The specs of `flux/pihole.yaml`'s two documents, `pihole` then `pihole-deferred`."""
    docs = list(yaml_fast.safe_load_all(_bundle(tag)["flux/pihole.yaml"]))
    assert [d["metadata"]["name"] for d in docs] == ["pihole", "pihole-deferred"]
    return [d["spec"] for d in docs]


def test_only_a_tags_own_directory_with_a_placeholder_substitutes():
    """`<tag>` holds a placeholder and `<tag>-deferred` none, as D3.3b's fixture asks (#5154).

    It goes red on a writer that gives `<tag>-deferred` `postBuild` because its tag
    substitutes.
    """
    main, deferred = _specs(_pihole(sentinel("email"), "plain"))
    assert main["dependsOn"] == [{"name": "cluster-vars"}]
    assert "postBuild" in main
    assert deferred["dependsOn"] == [{"name": "pihole"}]
    assert "postBuild" not in deferred


def test_a_placeholder_only_in_deferred_substitutes_there_and_counts_for_the_tag():
    """The reverse split: no edge on `<tag>`, while the footprint rule still counts it."""
    tag = _pihole("plain", sentinel("email"))
    main, deferred = _specs(tag)
    assert "dependsOn" not in main and "postBuild" not in main
    assert deferred["dependsOn"] == [{"name": "cluster-vars"}, {"name": "pihole"}]
    assert "postBuild" in deferred
    old = json.loads(_bundle(tag)[DIGESTS])
    new = json.loads(_bundle(tag, dotenv=DOTENV + "x=ENC[y]\n")[DIGESTS])
    assert old["pihole"]["footprint"] != new["pihole"]["footprint"]


def test_the_root_lists_every_file_in_flux():
    files = _bundle(_tag("bento-pdf"), _tag("littlelink", sentinel("email")))
    in_flux = sorted(
        p.removeprefix("flux/")
        for p in files
        if p.startswith("flux/") and p != FLUX_INDEX
    )
    assert in_flux == [
        "bento-pdf.yaml",
        "cluster-vars.yaml",
        "homelab-namespace.yaml",
        "littlelink.yaml",
        "soak.yaml",
    ]
    assert yaml_fast.safe_load(files[FLUX_INDEX])["resources"] == in_flux


def test_a_deferred_tag_gets_its_second_document_in_its_own_file():
    files = _bundle(_pihole("plain", "plain"))
    assert "flux/pihole-deferred.yaml" not in files
    assert yaml_fast.safe_load(files[FLUX_INDEX])["resources"] == [
        "cluster-vars.yaml",
        "homelab-namespace.yaml",
        "pihole.yaml",
        "soak.yaml",
    ]
    deferred = list(yaml_fast.safe_load_all(files["flux/pihole.yaml"]))[1]
    assert deferred["spec"]["path"] == "./cluster/pihole-deferred"


def test_two_renders_of_one_tree_write_identical_bundles(tmp_path, cluster_file):
    texts = []
    for name in ("first", "second"):
        out = tmp_path / name
        code = homelab_render.cluster(
            out,
            ["littlelink"],
            None,
            flux_owned=lambda: {"littlelink"},
            cluster_file=cluster_file,
        )
        assert code == RENDER_OK
        texts.append(
            {
                p.relative_to(out).as_posix(): p.read_bytes()
                for p in sorted(out.rglob("*"))
                if p.is_file() and not p.is_relative_to(out / "cluster" / "littlelink")
            }
        )
    # The image contexts are compared too, and are no bundle file (D5.4a1).
    assert texts[0] == texts[1]
    assert sorted(p for p in texts[0] if not p.startswith(("images/", "README"))) == [
        f"{CLUSTER_VARS_DIR}/{CLUSTER_VARS_FILE}",
        f"{CLUSTER_VARS_DIR}/kustomization.yaml",
        "cluster/homelab-namespace/kustomization.yaml",
        "cluster/homelab-namespace/limitrange.yaml",
        "cluster/homelab-namespace/namespace.yaml",
        "cluster/soak/configmap.yaml",
        "cluster/soak/job.yaml",
        "cluster/soak/kustomization.yaml",
        "cluster/soak/rbac.yaml",
        DIGESTS,
        "flux/cluster-vars.yaml",
        "flux/homelab-namespace.yaml",
        "flux/kustomization.yaml",
        "flux/littlelink.yaml",
        "flux/soak.yaml",
    ]
    assert set(json.loads(texts[0][DIGESTS])) == {
        "cluster-vars",
        "homelab-namespace",
        "littlelink",
        "soak",
        UNCLASSIFIED,
    }


def test_a_role_render_of_a_tag_flux_does_not_own_writes_no_bundle_is_clean(tmp_path):
    """pihole defers files, which only a published tag is refused for, and builds no image."""
    out = tmp_path / "out"
    assert homelab_render.cluster(out, ["pihole"], None, flux_owned=set) == RENDER_OK
    assert sorted(p.name for p in out.iterdir()) == ["cluster"]
    names = ["pihole", "pihole-deferred", "pihole-precheck", "pihole-verify"]
    assert sorted(p.name for p in (out / "cluster").iterdir()) == names


def test_a_flux_owned_tag_that_defers_files_publishes_both_kustomizations(
    tmp_path, cluster_file
):
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        ["pihole"],
        None,
        flux_owned=lambda: {"pihole"},
        cluster_file=cluster_file,
    )
    assert code == RENDER_OK
    docs = yaml_fast.safe_load_all((out / "flux" / "pihole.yaml").read_text())
    names = ["pihole", "pihole-deferred", "pihole-precheck", "pihole-verify"]
    assert [d["metadata"]["name"] for d in docs] == names


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
        "cluster-vars": set(),
        "homelab-namespace": set(),
        "soak": set(),
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
    kinds = {"cluster-vars"}
    rules = {"cluster-vars": lambda f: f.substituting}
    edited = {**BASE, "cluster/cluster-vars/secret.yaml": b"v2"}
    base = {**BASE, "cluster/cluster-vars/secret.yaml": b"v1"}
    old = digests(base, TAGS, substituting={"bento-pdf"}, kinds=kinds, rules=rules)
    new = digests(edited, TAGS, substituting={"bento-pdf"}, kinds=kinds, rules=rules)
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
    # `soak` joins by C2's rule, because the set holds a tag.
    assert moved.names == {"bento-pdf", "soak"} and moved.widened == ""


# -- the cluster-vars kind (D3.3a) -----------------------------------------------------------


def test_the_cluster_vars_kustomization_decrypts_with_the_cluster_key():
    """With a substituting tag beside it, `cluster-vars` still gets no `postBuild` or edge."""
    files = _bundle(_tag("bento-pdf", sentinel("email")))
    expected = _expected("cluster-vars")
    expected["spec"]["decryption"] = {
        "provider": "sops",
        "secretRef": {"name": "sops-age"},
    }
    assert yaml_fast.safe_load(files["flux/cluster-vars.yaml"]) == expected


def test_the_cluster_vars_directory_generates_the_secret_from_a_byte_copy():
    files = _bundle(_tag("bento-pdf"))
    assert files[f"{CLUSTER_VARS_DIR}/{CLUSTER_VARS_FILE}"] == DOTENV
    assert yaml_fast.safe_load(files[f"{CLUSTER_VARS_DIR}/kustomization.yaml"]) == {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": "flux-system",
        "generatorOptions": {"disableNameSuffixHash": True},
        "secretGenerator": [
            {"name": "homelab-cluster-vars", "envs": [CLUSTER_VARS_FILE]}
        ],
    }


def test_a_cluster_file_edit_moves_only_a_substituting_tags_footprint():
    tags = (_tag("bento-pdf", sentinel("email")), _tag("sonarr"))
    old = json.loads(_bundle(*tags)[DIGESTS])
    new = json.loads(_bundle(*tags, dotenv=DOTENV + "x=ENC[y]\n")[DIGESTS])
    assert old["bento-pdf"]["footprint"] != new["bento-pdf"]["footprint"]
    assert old["bento-pdf"]["own"] == new["bento-pdf"]["own"]
    assert old["sonarr"] == new["sonarr"]
    assert old["cluster-vars"]["own"] != new["cluster-vars"]["own"]
    assert new[UNCLASSIFIED]["paths"] == ["README.md"]


def test_a_shared_only_render_holds_the_root_and_cluster_vars_and_no_tag(
    tmp_path, cluster_file
):
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out, None, None, cluster_file=cluster_file, shared_only=True
    )
    assert code == RENDER_OK
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*.*")) == [
        "README.md",
        f"{CLUSTER_VARS_DIR}/{CLUSTER_VARS_FILE}",
        f"{CLUSTER_VARS_DIR}/kustomization.yaml",
        "cluster/homelab-namespace/kustomization.yaml",
        "cluster/homelab-namespace/limitrange.yaml",
        "cluster/homelab-namespace/namespace.yaml",
        "cluster/soak/configmap.yaml",
        "cluster/soak/job.yaml",
        "cluster/soak/kustomization.yaml",
        "cluster/soak/rbac.yaml",
        DIGESTS,
        "flux/cluster-vars.yaml",
        "flux/homelab-namespace.yaml",
        "flux/kustomization.yaml",
        "flux/soak.yaml",
    ]
    assert yaml_fast.safe_load((out / FLUX_INDEX).read_text())["resources"] == [
        "cluster-vars.yaml",
        "homelab-namespace.yaml",
        "soak.yaml",
    ]
    assert set(json.loads((out / DIGESTS).read_text())) == {
        "cluster-vars",
        "homelab-namespace",
        "soak",
        UNCLASSIFIED,
    }


def test_a_render_without_the_cluster_file_is_refused_and_writes_nothing(
    tmp_path, capsys
):
    out = tmp_path / "out"
    out.mkdir()
    missing = tmp_path / "absent" / CLUSTER_VARS_FILE
    code = homelab_render.cluster(
        out, None, None, cluster_file=missing, shared_only=True
    )
    assert code == RENDER_REFUSED
    assert "cannot copy the cluster file" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_shared_only_beside_role_is_a_usage_error(tmp_path):
    argv = ["cluster", "--out", str(tmp_path), "--shared-only", "--role", "littlelink"]
    with pytest.raises(SystemExit) as exc:
        homelab_render.main(argv)
    assert exc.value.code == RENDER_BAD_ARGS
