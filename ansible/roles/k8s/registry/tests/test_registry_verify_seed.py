"""The push verify Job's seed: it copies one platform of the python Job image into the push store.

Two fake registries run on local ports, one as the source and one as the push store. The
source demands a bearer token and redirects each blob to a CDN path, as Docker Hub does, so the
tests pin the transport the Job uses against Docker Hub, not only its verdict.

Run: uv run pytest ansible/roles/k8s/registry/tests/test_registry_verify_seed.py
"""

import json

import pytest

import registry_verify
from _registry_fakes import digest, serving

REPO = "registry-selftest"
TAG = "python-0123456789ab"
CONFIG = b'{"architecture":"amd64","os":"linux"}'
LAYER = b"layer bytes"
OTHER_LAYER = b"arm64 layer bytes"
IMAGE = registry_verify.MANIFEST_TYPE
INDEX = registry_verify.INDEX_TYPES[0]


def _manifest(config: bytes, layer: bytes) -> bytes:
    def blob(data: bytes, media: str) -> dict:
        return {"mediaType": media, "digest": digest(data), "size": len(data)}

    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": IMAGE,
            "config": blob(config, registry_verify.CONFIG_TYPE),
            "layers": [blob(layer, registry_verify.LAYER_TYPE)],
        }
    ).encode()


AMD64 = _manifest(CONFIG, LAYER)
ARM64 = _manifest(b'{"architecture":"arm64"}', OTHER_LAYER)


def _index(*platforms: tuple[bytes, str]) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": INDEX,
            "manifests": [
                {
                    "mediaType": IMAGE,
                    "digest": digest(body),
                    "size": len(body),
                    "platform": {"os": "linux", "architecture": arch},
                }
                for body, arch in platforms
            ],
        }
    ).encode()


INDEX_BODY = _index((ARM64, "arm64"), (AMD64, "amd64"))


@pytest.fixture
def source():
    """A Docker Hub stand-in holding a two-platform python image under `library/python`."""
    with serving() as server:
        server.token_realm = f"{server.url}/token"
        server.redirect_blobs = True
        server.hold("library/python", "3.14-alpine", INDEX_BODY, INDEX)
        for body in (AMD64, ARM64):
            server.hold("library/python", digest(body), body, IMAGE)
        for data in (CONFIG, LAYER, OTHER_LAYER):
            server.blobs[digest(data)] = data
        yield server


@pytest.fixture
def store():
    with serving() as server:
        yield server


def _ref(source) -> str:
    host = source.url.removeprefix("http://")
    return f"{host}/library/python:3.14-alpine@{digest(INDEX_BODY)}"


def _seed(source, store) -> list[str]:
    return registry_verify.seed(_ref(source), "linux/amd64", store.url, REPO, TAG)


def test_the_seed_copies_only_the_platform_image_and_tags_it(source, store):
    lines = _seed(source, store)

    assert store.manifests[f"{REPO}:{TAG}"] == AMD64
    assert store.media[f"{REPO}:{TAG}"] == IMAGE
    assert set(store.blobs) == {digest(CONFIG), digest(LAYER)}
    assert lines[-1].startswith(f"seeded {REPO}:{TAG} as {digest(AMD64)}")


def test_the_token_is_not_sent_to_the_cdn_a_blob_redirects_to(source, store):
    """Docker Hub's CDN serves a signed URL, which a second credential can make it refuse."""
    _seed(source, store)

    assert source.cdn_auth == ["", ""]


def test_a_tag_the_store_serves_reads_nothing_upstream(store):
    """A seed already made costs one request, so a WAN outage does not fail the push Job."""
    store.hold(REPO, TAG, AMD64, IMAGE)
    unreachable = "127.0.0.1:9/library/python@sha256:" + "0" * 64

    lines = registry_verify.seed(unreachable, "linux/amd64", store.url, REPO, TAG)

    assert lines == [f"seed {REPO}:{TAG} already served"]


def test_a_blob_the_store_holds_is_not_copied_again(source, store):
    store.blobs[digest(LAYER)] = LAYER

    lines = _seed(source, store)

    assert store.uploads == 1
    assert f"seed blob {digest(LAYER)} already present" in lines


def test_a_source_serving_bytes_off_their_digest_fails_the_seed(source, store):
    source.corrupt = _index((AMD64, "amd64"))

    with pytest.raises(registry_verify.Failed, match="served bytes of another digest"):
        _seed(source, store)
    assert f"{REPO}:{TAG}" not in store.manifests


def test_an_index_without_the_platform_fails_the_seed(source, store):
    with pytest.raises(registry_verify.Failed, match="names no linux/s390x manifest"):
        registry_verify.seed(_ref(source), "linux/s390x", store.url, REPO, TAG)


@pytest.mark.parametrize(
    "ref, want",
    [
        (
            "python:3.14-alpine@sha256:ab",
            ("https://registry-1.docker.io", "library/python", "sha256:ab"),
        ),
        (
            "docker.io/grafana/grafana:12@sha256:ab",
            ("https://registry-1.docker.io", "grafana/grafana", "sha256:ab"),
        ),
        ("ghcr.io/x/y:1@sha256:ab", ("https://ghcr.io", "x/y", "sha256:ab")),
        ("localhost:5000/y@sha256:ab", ("http://localhost:5000", "y", "sha256:ab")),
    ],
)
def test_an_image_ref_resolves_to_its_registry_repository_and_digest(ref, want):
    assert registry_verify.parse_image(ref) == want


def test_an_image_not_pinned_by_digest_is_refused():
    with pytest.raises(registry_verify.Failed, match="not pinned by a sha256 digest"):
        registry_verify.parse_image("python:3.14-alpine")
