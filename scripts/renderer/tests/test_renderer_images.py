"""D5.4a1: `homelab-render cluster` writes each image context and `DIR/images/tags.json`.

One placeholder render of every builder role, plus littlelink as a flux-owned tag so a bundle
is written too, backs most checks. Each tag in `tags.json` is recomputed by ansible-core's
`Templar` over the files as written, with image-builder's own template and expression, so a
file written from anything but the hashed `data` fails here. Each check has a red half.

Run: uv run pytest scripts/renderer/tests/test_renderer_images.py
"""

import json
import shutil

import pytest

from _image_builder_tag import templar_content_tag
from dataclasses import replace

from lib.exit_codes import RENDER_FAILED, RENDER_OK, RENDER_REFUSED
from lib.render_guard import BUILT_IMAGE_TAG_STUBS
from renderer import homelab_render
from renderer.renderer_lib import cluster, images, placeholders
from renderer.renderer_lib.environment import RenderError

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


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    root = tmp_path_factory.mktemp("images")
    dotenv = root / "cluster-vars"
    dotenv.write_text("x=ENC[AES256_GCM,data:eA==,type:str]\n")
    code = homelab_render.cluster(
        root / "out",
        [*images.builder_roles(), "littlelink"],
        None,
        flux_owned=lambda: {"littlelink"},
        cluster_file=dotenv,
    )
    assert code == RENDER_OK
    return root / "out"


def _tags(out) -> dict[str, str]:
    return json.loads((out / images.TAGS_FILE).read_text())


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
    directories = {
        d.name: {f.name: f.read_text() for f in d.iterdir() if f.is_file()}
        for d in (out / "cluster").iterdir()
    }
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
