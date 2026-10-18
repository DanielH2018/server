"""D5.4a1: `homelab-render cluster` writes each image context and `DIR/images/tags.json`.

One placeholder render of every builder role, plus littlelink as a flux-owned tag so a bundle
is written too, backs most checks. It marks ical-proxy flux-owned as well: no live flux-owned
tag consumes a built image yet, so ical-proxy is the fixture role for D5.4b's GHCR refs. Each tag in `tags.json` is recomputed by ansible-core's
`Templar` over the files as written, with image-builder's own template and expression, so a
file written from anything but the hashed `data` fails here. Each check has a red half.

Run: uv run pytest scripts/renderer/tests/test_renderer_images.py
"""

import json
import re
import shutil

import pytest

from _image_builder_tag import templar_content_tag
from dataclasses import replace

from lib.exit_codes import RENDER_FAILED, RENDER_OK, RENDER_REFUSED
from lib.render_guard import BUILT_IMAGE_TAG_STUBS
from deploy_tools import ci_images
from lib import yaml_fast
from renderer import homelab_render
from renderer.renderer_lib import cluster, images, placeholders
from renderer.renderer_lib.environment import RenderError
from renderer.shadow_diff import stub_file_text

# The ten images k8s/image-builder builds; n8n builds two.
TEN = frozenset(
    {
        "code-server",
        "homelab-mcp",
        "ical-proxy",
        "karakeep",
        "n8n",
        "n8n-runners",
        "nut",
        "pi-peer-backup",
        "terraria",
        "valheim",
    }
)
# The builder role the fixture render marks flux-owned.
OWNED = "ical-proxy"
NO_POLICY = re.compile(r"\n *imagePullPolicy: Always")


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    root = tmp_path_factory.mktemp("images")
    dotenv = root / "cluster-vars"
    dotenv.write_text("x=ENC[AES256_GCM,data:eA==,type:str]\n")
    code = homelab_render.cluster(
        root / "out",
        [*images.builder_roles(), "littlelink"],
        None,
        flux_owned=lambda: {"littlelink", OWNED},
        cluster_file=dotenv,
    )
    assert code == RENDER_OK
    return root / "out"


def _tags(out) -> dict[str, str]:
    return json.loads((out / images.TAGS_FILE).read_text())


def _container(path, name: str) -> dict:
    """The container `name` of the Deployment in `path`."""
    (doc,) = [d for d in yaml_fast.safe_load_all(path.read_text()) if d]
    (found,) = [
        c for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] == name
    ]
    return found


def _directories(out) -> dict[str, dict[str, str]]:
    return {
        d.name: {f.name: f.read_text() for f in d.iterdir() if f.is_file()}
        for d in (out / "cluster").iterdir()
    }


def _nut_on_ghcr(out) -> str:
    """nut's rendered Deployment with its ref moved to GHCR and its `Always` kept."""
    text = (out / "cluster" / "nut" / "deployment.yaml").read_text()
    return text.replace("localhost:5000/nut:", "ghcr.io/danielh2018/nut:")


def _oracle_tag(context, name: str) -> str:
    """The content tag ansible-core computes over the context directory `context` as written.

    The Dockerfile goes through image-builder's template lookup again and every other file
    through its file lookup, so the written Dockerfile must carry no Jinja for the round trip
    to hold.
    """
    dockerfile = context / "Dockerfile"
    text = dockerfile.read_text()
    assert "{{" not in text and "{%" not in text, dockerfile
    variables = {
        "image_builder_name": name,
        "k8s_namespace": "homelab",
        "image_builder_dockerfile": str(dockerfile),
        "image_builder_context": {},
        "image_builder_context_files": {
            str(p): p.name for p in context.iterdir() if p != dockerfile
        },
    }
    return templar_content_tag(variables)


def test_the_render_writes_all_ten_contexts_and_tags_json(out):
    assert set(_tags(out)) == TEN
    written = {p.name for p in (out / images.IMAGES_DIR).iterdir() if p.is_dir()}
    assert written == TEN
    for name in TEN:
        assert (out / images.IMAGES_DIR / name / "Dockerfile").is_file(), name


def test_a_render_of_one_unrelated_tag_still_carries_all_ten():
    """The publish job renders only the flux-owned tags, none of which builds an image."""
    keys = homelab_render.secret_keys()
    assert set(placeholders.render_placeholders(["speedtest"], keys).images) == TEN


@pytest.mark.parametrize("name", sorted(TEN))
def test_each_tag_equals_the_content_tag_over_its_written_context(out, name):
    context = out / images.IMAGES_DIR / name
    assert _oracle_tag(context, name) == _tags(out)[name]


def test_a_byte_changed_in_a_written_context_moves_the_tag(out, tmp_path):
    """The red half: the tag describes these bytes, so one changed byte must not match."""
    context = shutil.copytree(out / images.IMAGES_DIR / "nut", tmp_path / "nut")
    dockerfile = context / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "#\n")
    assert _oracle_tag(context, "nut") != _tags(out)["nut"]


def test_every_consumer_ref_equals_tags_json(out):
    tags = _tags(out)
    directories = _directories(out)
    named = {
        name
        for files in directories.values()
        for text in files.values()
        for name, tag in tags.items()
        if f"/{name}:{tag}" in text
    }
    assert named == TEN
    assert images.ref_mismatches(directories, tags) == []


def test_a_consumer_rendered_from_the_guard_stubs_is_flagged(out):
    """The red half: `BUILT_IMAGE_TAG_STUBS` names an image no build produced."""
    stubs = {
        **placeholders.placeholder_vars(homelab_render.secret_keys()),
        "k8s_built_image_tags": BUILT_IMAGE_TAG_STUBS,
    }
    rendered = cluster.render_tag("nut", stubs)
    problems = images.ref_mismatches({"nut": rendered.files}, _tags(out))
    assert any(BUILT_IMAGE_TAG_STUBS["nut"] in p for p in problems), problems


def test_a_matching_ref_and_a_latest_ref_are_clean():
    files = {"d.yaml": "image: r/nut:sha-0123456789ab\nimage: r/n8n:latest\n"}
    tags = {"nut": "sha-0123456789ab", "n8n": "sha-ba9876543210"}
    assert images.ref_mismatches({"nut": files}, tags) == []


def test_no_kustomization_path_reaches_images(out):
    files = {
        p.relative_to(out).as_posix(): p.read_text()
        for p in out.rglob("*")
        if p.is_file() and p.suffix == ".yaml"
    }
    assert "flux/littlelink.yaml" in files
    assert images.paths_into_images(files) == []


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("flux/nut.yaml", "kind: Kustomization\nspec:\n  path: ./images/nut\n"),
        ("flux/nut.yaml", "kind: Kustomization\nspec:\n  path: ./cluster/../images\n"),
        ("cluster/nut/kustomization.yaml", "resources:\n  - ../../images/nut/x.yaml\n"),
        ("flux/nut.yaml", "kind: Kustomization\nspec:\n  path: /images/nut\n"),
        ("flux/kustomization.yaml", "resources:\n  - ../images/nut\n"),
        ("cluster/nut/kustomization.yaml", "components:\n  - ../../images/nut\n"),
        (
            "cluster/nut/kustomization.yaml",
            "patches:\n  - path: ../../images/nut/p.yaml\n",
        ),
    ],
)
def test_a_kustomization_path_into_images_is_flagged(path, text):
    assert images.paths_into_images({path: text}) != []


def test_a_file_that_is_not_yaml_is_not_read():
    # The bundle's README.md is Markdown that a YAML parser rejects.
    assert images.paths_into_images({"README.md": "# Title\n\n- a: b: c\n"}) == []


def test_a_kustomization_path_into_cluster_is_clean():
    files = {
        "flux/nut.yaml": "kind: Kustomization\nspec:\n  path: ./cluster/nut\n",
        "cluster/nut/kustomization.yaml": "resources:\n  - deployment.yaml\n",
    }
    assert images.paths_into_images(files) == []


def test_a_context_that_reads_a_secret_is_flagged():
    keys = homelab_render.secret_keys()
    leak = images.BuiltImage(
        "nut", {"Dockerfile": f"ENV P={placeholders.sentinel(keys[0])}\n"}, "sha-0"
    )
    with pytest.raises(RenderError, match="reads the secrets"):
        placeholders.render_placeholders([], keys, contexts=lambda *_: {"nut": leak})


def test_an_images_directory_left_by_an_earlier_render_is_flagged(tmp_path):
    out = tmp_path / "out"
    (out / images.IMAGES_DIR).mkdir(parents=True)
    (out / images.TAGS_FILE).write_text("{}\n")
    assert homelab_render.cluster(out, ["speedtest"], None) == RENDER_REFUSED
    assert not (out / "cluster").exists()


def _edited_render(edit):
    """A `render` for `homelab_render.cluster`: the real placeholder render, then `edit`."""

    def render(tags, keys):
        return edit(placeholders.render_placeholders(tags, keys))

    return render


def _refused_writing_nothing(tmp_path, edit) -> None:
    out = tmp_path / "out"
    code = homelab_render.cluster(out, ["nut"], None, render=_edited_render(edit))
    assert code == RENDER_FAILED
    assert not out.exists() or not any(out.iterdir())


def test_a_render_whose_ref_differs_from_tags_json_writes_nothing(tmp_path):
    """The red half end to end: `check_images` must refuse before any write."""

    def retag(result):
        nut = replace(result.images["nut"], tag="sha-000000000000")
        return replace(result, images={**result.images, "nut": nut})

    _refused_writing_nothing(tmp_path, retag)


def test_a_render_whose_kustomization_reaches_images_writes_nothing(tmp_path):
    def reach(result):
        (tag,) = result.tags
        files = {
            **tag.files,
            "kustomization.yaml": "resources:\n  - ../../images/nut\n",
        }
        return replace(result, tags=[replace(tag, files=files)])

    _refused_writing_nothing(tmp_path, reach)


def test_a_flux_owned_consumer_pulls_its_ghcr_tag_if_not_present(out):
    container = _container(out / "cluster" / OWNED / "deployment.yaml", OWNED)
    assert container["image"] == f"ghcr.io/danielh2018/{OWNED}:{_tags(out)[OWNED]}"
    assert container["imagePullPolicy"] == "IfNotPresent"


def test_an_ansible_owned_consumer_keeps_the_in_cluster_registry(out):
    container = _container(out / "cluster" / "nut" / "deployment.yaml", "nut")
    assert container["image"] == f"localhost:5000/nut:{_tags(out)['nut']}"
    assert container["imagePullPolicy"] == "Always"


def test_a_stub_render_points_a_flux_owned_consumer_at_ghcr_as_placeholder_mode_does(
    out, tmp_path
):
    stubs = tmp_path / "stubs.yml"
    stubs.write_text(stub_file_text())
    code = homelab_render.cluster(
        tmp_path / "out", [OWNED], stubs, flux_owned=lambda: {OWNED}
    )
    assert code == RENDER_OK
    path = f"cluster/{OWNED}/deployment.yaml"
    container = _container(tmp_path / "out" / path, OWNED)
    assert container["image"] == f"ghcr.io/danielh2018/{OWNED}:{_tags(out)[OWNED]}"
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert (tmp_path / "out" / path).read_text() == (out / path).read_text()


def test_no_ghcr_ref_in_the_render_pulls_always(out):
    directories = _directories(out)
    refs = {
        ref
        for files in directories.values()
        for text in files.values()
        for ref in images.GHCR_REF.findall(text)
    }
    assert f"ghcr.io/danielh2018/{OWNED}:{_tags(out)[OWNED]}" in refs
    assert images.pull_policy_offences(directories) == []


@pytest.mark.parametrize(
    ("edit", "policy"),
    [(lambda text: text, "Always"), (lambda text: NO_POLICY.sub("", text), "unset")],
)
def test_a_ghcr_ref_that_pulls_always_or_sets_no_policy_is_flagged(out, edit, policy):
    """The red half: nut's Deployment on GHCR with the in-cluster registry's `Always`."""
    files = {"nut": {"deployment.yaml": edit(_nut_on_ghcr(out))}}
    (problem,) = images.pull_policy_offences(files)
    assert "ghcr.io/danielh2018/nut:sha-" in problem
    assert f"imagePullPolicy {policy}," in problem


def test_ghcr_text_moves_only_built_refs_and_adds_a_missing_policy():
    text = (
        "kind: Pod\nspec:\n  containers:\n"
        "    - name: a\n      image: localhost:5000/nut:sha-0123456789ab\n      ports: []\n"
        "    - image: localhost:5000/probe:probe\n      imagePullPolicy: Always\n"
        "    - image: localhost:5000/n8n:sha-ba9876543210\n      imagePullPolicy: Always\n"
    )
    tags = {"nut": "sha-0123456789ab", "n8n": "sha-ba9876543210"}
    assert images.ghcr_text(text, tags, "pod.yaml") == (
        "kind: Pod\nspec:\n  containers:\n"
        "    - name: a\n      image: ghcr.io/danielh2018/nut:sha-0123456789ab\n"
        "      imagePullPolicy: IfNotPresent\n      ports: []\n"
        "    - image: localhost:5000/probe:probe\n      imagePullPolicy: Always\n"
        "    - image: ghcr.io/danielh2018/n8n:sha-ba9876543210\n"
        "      imagePullPolicy: IfNotPresent\n"
    )


def test_the_ghcr_prefix_is_where_ci_images_pushes():
    assert f"{images.GHCR}/nut:sha-0" == ci_images.ref("nut", "sha-0")


def test_a_render_whose_ghcr_ref_pulls_always_writes_nothing(tmp_path):
    """The red half end to end: `check_images` refuses before any write."""

    def to_ghcr(result):
        (tag,) = result.tags
        files = {
            name: text.replace("localhost:5000/nut:", "ghcr.io/danielh2018/nut:")
            for name, text in tag.files.items()
        }
        return replace(result, tags=[replace(tag, files=files)])

    _refused_writing_nothing(tmp_path, to_ghcr)
