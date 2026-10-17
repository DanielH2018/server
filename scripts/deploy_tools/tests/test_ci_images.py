"""The promote workflow's `images` job computes image-builder's content tag and pushes each once.

The tag half compares `ci_images.content_tag` with image-builder's own expression, evaluated
by ansible-core's `Templar` (`_image_builder_tag`, the oracle of
`ansible/tests/deploy/test_image_builder_content_tag.py`), and then with every context a real
render writes. The push half drives `ci_images.push` with a fake registry and a fake build.

Run: uv run pytest scripts/deploy_tools/tests/test_ci_images.py
"""

import json
import shutil
from typing import cast

import pytest

from _image_builder_tag import IMAGE_BUILDER, templar_content_tag, templar_value
from deploy_tools import ci_images
from deploy_tools.ci_images import Answer, Refused
from lib.exit_codes import RENDER_OK
from renderer import homelab_render
from renderer.renderer_lib import images

DIGEST = "sha256:" + "ab" * 32
SERVED = Answer(200, DIGEST)
# Members the real render must yield: the multi-file context, and nut, whose LAN source is
# the reason D5.4b keeps it last.
NAMED = {"homelab-mcp", "nut"}


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    """A placeholder render of every role that builds an image."""
    root = tmp_path_factory.mktemp("ci-images")
    dotenv = root / "cluster-vars"
    dotenv.write_text("x=ENC[AES256_GCM,data:eA==,type:str]\n")
    code = homelab_render.cluster(
        root / "out",
        images.builder_roles(),
        None,
        flux_owned=lambda: set(),
        cluster_file=dotenv,
    )
    assert code == RENDER_OK
    return root / "out"


@pytest.fixture
def copy(out, tmp_path):
    """A writable copy of the render's `images/`, so a test can damage it."""
    shutil.copytree(out / ci_images.IMAGES_DIR, tmp_path / ci_images.IMAGES_DIR)
    return tmp_path


def test_the_tag_matches_image_builders_expression(tmp_path):
    """Mixed-case names, non-ASCII text and a file with no trailing newline.

    `dictsort` orders `app.py` before `Dockerfile`, `to_json` escapes `é`, and the file lookup
    strips the trailing newline the block scalar puts back. Each is a way a restated hash
    drifts from the role's.
    """
    dockerfile = tmp_path / "Dockerfile.j2"
    dockerfile.write_text("FROM alpine:3.24\nCOPY app.py /\n")
    app = tmp_path / "app.py"
    app.write_text("print('café')")
    variables = {
        "image_builder_name": "demo",
        "k8s_namespace": "homelab",
        "image_builder_dockerfile": str(dockerfile),
        "image_builder_context": {},
        "image_builder_context_files": {str(app): "app.py", str(dockerfile): "Zeta"},
    }
    data = cast(
        dict[str, str],
        templar_value(
            "{{ (lookup('template', 'context-configmap.yaml.j2') | from_yaml)['data'] }}",
            variables,
            IMAGE_BUILDER,
        ),
    )
    assert list(data) != sorted(data), (
        "the fixture no longer exercises dictsort's case folding"
    )
    assert ci_images.content_tag(data) == templar_content_tag(variables)


def test_every_rendered_context_is_planned_under_its_tags_json_tag(out):
    planned = dict(ci_images.plan(out))
    tags = json.loads((out / images.TAGS_FILE).read_text())
    assert planned == tags
    assert NAMED <= set(planned), f"the render no longer yields {NAMED - set(planned)}"


def test_a_changed_byte_in_a_context_is_refused(copy):
    dockerfile = copy / "images" / "nut" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "RUN true\n")
    with pytest.raises(Refused, match="nut's context hashes to sha-"):
        ci_images.plan(copy)


def test_a_context_tags_json_does_not_name_is_refused(copy):
    shutil.copytree(copy / "images" / "nut", copy / "images" / "stray")
    with pytest.raises(Refused, match="stray has no entry"):
        ci_images.plan(copy)


def test_a_tags_json_entry_with_no_context_is_refused(copy):
    shutil.rmtree(copy / "images" / "nut")
    with pytest.raises(Refused, match="names nut, which has no context"):
        ci_images.plan(copy)


def test_a_render_with_no_images_is_refused(tmp_path):
    with pytest.raises(Refused, match="wrote no image contexts"):
        ci_images.plan(tmp_path)


class Fakes:
    """A registry that answers `before` until a build pushes, then `after`; and the build."""

    def __init__(self, before: Answer, after: Answer = SERVED, digest=DIGEST):
        self.before, self.after, self.digest = before, after, digest
        self.built: list[str] = []

    def lookup(self, name: str, tag: str) -> Answer:
        return self.after if name in self.built else self.before

    def build(self, name: str, tag: str, context) -> str:
        assert (context / "Dockerfile").is_file()
        self.built.append(name)
        return self.digest


def _push(out, fakes: Fakes) -> list[str]:
    return ci_images.push(out, fakes.lookup, fakes.build)


def test_a_served_tag_is_success_and_is_never_rebuilt(out):
    fakes = Fakes(SERVED)
    assert _push(out, fakes) == []
    assert fakes.built == []


def test_an_absent_tag_is_built_pushed_and_read_back(out):
    fakes = Fakes(Answer(404))
    assert _push(out, fakes) == []
    assert set(fakes.built) == set(dict(ci_images.plan(out)))


def test_a_registry_refusal_builds_nothing_and_fails(out):
    fakes = Fakes(Answer(403))
    problems = _push(out, fakes)
    assert fakes.built == []
    assert problems and all("HTTP 403" in p for p in problems)


def test_a_failed_build_fails_its_image_and_the_others_still_build(out):
    fakes = Fakes(Answer(404), digest="")
    problems = _push(out, fakes)
    assert len(fakes.built) == len(problems) == len(ci_images.plan(out))
    assert all("the build or its push failed" in p for p in problems)


def test_a_push_served_at_another_digest_than_the_build_metadata_names_is_success(out):
    # The default `docker` driver's metadata digest can be the config digest, not the
    # manifest digest GHCR serves.
    fakes = Fakes(Answer(404), after=Answer(200, "sha256:" + "cd" * 32))
    assert _push(out, fakes) == []


def test_a_push_the_registry_does_not_serve_fails(out):
    fakes = Fakes(Answer(404), after=Answer(404))
    problems = _push(out, fakes)
    assert problems and all("but the registry answers HTTP 404" in p for p in problems)


class ExpiringRegistry(ci_images.Registry):
    """GHCR's token endpoint and manifests, where a build outlives every token issued before it."""

    def __init__(self):
        super().__init__("actor", "password")
        self.issued = 0
        self.valid: set[str] = set()
        self.served: set[str] = set()

    def _open(self, request):
        url = request.full_url
        if "/token?" in url:
            self.issued += 1
            self.valid.add(f"t{self.issued}")
            return 200, {}, json.dumps({"token": f"t{self.issued}"}).encode()
        bearer = str(request.get_header("Authorization", "")).removeprefix("Bearer ")
        if bearer not in self.valid:
            return 401, {}, b""
        name = url.split("/v2/", 1)[1].split("/")[1]
        if name in self.served:
            return 200, {"Docker-Content-Digest": DIGEST}, b""
        return 404, {}, b""

    def build(self, name: str, tag: str, context) -> str:
        self.valid.clear()
        self.served.add(name)
        return DIGEST


def test_the_read_back_after_a_build_asks_for_a_fresh_token(out):
    registry = ExpiringRegistry()
    assert ci_images.push(out, registry.lookup, registry.build) == []
    assert registry.issued == 2 * len(ci_images.plan(out))
