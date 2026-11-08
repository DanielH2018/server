"""The fresh-cluster case's files, archives and verdict (#7555, `fresh_cluster_drill.py`).

Run: uv run pytest scripts/renderer/tests/test_fresh_cluster_drill.py
"""

import ipaddress
import json
import tarfile

import pytest
from lib import yaml_fast
from renderer import fresh_cluster_drill as fresh

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
FAKE_KEY = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"
URL = "oci://registry.homelab.svc.cluster.local:5000/homelab-manifests"


# --- The images -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "name"),
    [
        (
            f"ghcr.io/fluxcd/source-controller:v1.9.6@{DIGEST}",
            f"ghcr.io/fluxcd/source-controller@{DIGEST}",
        ),
        (f"registry:3.1.2@{DIGEST}", f"docker.io/library/registry@{DIGEST}"),
        (f"grafana/alloy@{DIGEST}", f"docker.io/grafana/alloy@{DIGEST}"),
        (f"localhost:5000/app:1@{DIGEST}", f"localhost:5000/app@{DIGEST}"),
    ],
)
def test_containerd_name_drops_the_tag_and_normalizes_the_registry(ref, name):
    assert fresh.containerd_name(ref) == name


def test_containerd_name_refuses_a_reference_with_no_digest():
    with pytest.raises(ValueError, match="not pinned"):
        fresh.containerd_name("registry:3.1.2")


def test_the_kit_holds_the_three_controllers_and_the_registry_by_digest():
    images = fresh.kit_images()
    repos = {fresh.containerd_name(ref).split("@")[0] for ref in images}
    assert repos == {
        "ghcr.io/fluxcd/source-controller",
        "ghcr.io/fluxcd/kustomize-controller",
        "ghcr.io/fluxcd/notification-controller",
        "docker.io/library/registry",
    }


def _layout(tmp_path, *digests):
    (tmp_path / "blobs" / "sha256").mkdir(parents=True)
    (tmp_path / "blobs" / "sha256" / ("a" * 64)).write_text("{}")
    (tmp_path / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}')
    manifests = [
        {"mediaType": "application/vnd.oci.image.index.v1+json", "digest": d, "size": 1}
        for d in digests
    ]
    (tmp_path / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": manifests})
    )
    return tmp_path


def test_an_archive_names_the_pinned_index_for_containerd(tmp_path):
    layout = _layout(tmp_path / "layout", DIGEST)
    out = tmp_path / "image.tar"
    fresh.archive(layout, f"registry:3.1.2@{DIGEST}", out)
    with tarfile.open(out) as tar:
        assert set(tar.getnames()) == {
            "oci-layout",
            "index.json",
            "blobs",
            "blobs/sha256",
            f"blobs/sha256/{'a' * 64}",
        }
        member = tar.extractfile("index.json")
        assert member is not None
        index = json.load(member)
    assert index["manifests"][0]["annotations"] == {
        fresh.CONTAINERD_NAME: f"docker.io/library/registry@{DIGEST}"
    }


@pytest.mark.parametrize(
    ("digests"), [(OTHER,), (DIGEST, OTHER), ()], ids=["other digest", "two", "none"]
)
def test_a_layout_is_flagged_unless_it_holds_only_the_pinned_digest(tmp_path, digests):
    with pytest.raises(ValueError, match="not the pinned"):
        fresh.name_layout(_layout(tmp_path, *digests), f"registry:3.1.2@{DIGEST}")


# --- What the case applies ------------------------------------------------------------


def _docs(files):
    return [d for text in files.values() for d in yaml_fast.safe_load_all(text) if d]


def test_the_registry_render_loses_its_node_pin_and_keeps_its_claim_and_address():
    docs = _docs(fresh.registry_files())
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    assert "registry" in deployments
    for doc in deployments.values():
        assert "nodeSelector" not in doc["spec"]["template"]["spec"]
    volumes = deployments["registry"]["spec"]["template"]["spec"]["volumes"]
    assert volumes[0]["persistentVolumeClaim"]["claimName"] == "registry-data"
    service = next(d for d in docs if d["kind"] == "Service")
    subnet = ipaddress.ip_network(fresh.kind_config()["networking"]["serviceSubnet"])
    assert ipaddress.ip_address(service["spec"]["clusterIP"]) in subnet


def test_the_prerequisites_hold_the_namespace_the_class_and_a_stand_in_per_claim_class():
    registry = fresh.registry_files()
    classes = fresh.claim_classes(registry)
    assert "longhorn-nobackup" in classes
    docs = _docs(fresh.prereq_files(classes))
    named = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert {("Namespace", "homelab"), ("PriorityClass", "homelab-critical")} <= named
    stand_ins = [d for d in docs if d["kind"] == "StorageClass"]
    assert {d["metadata"]["name"] for d in stand_ins} == classes
    assert {d["provisioner"] for d in stand_ins} == {fresh.STAND_IN_PROVISIONER}


def test_the_source_follows_the_roles_own_registry_address_and_verifies_the_key():
    source, secret = fresh.break_glass_source(FAKE_KEY)
    assert source["spec"]["url"] == URL
    assert source["spec"]["verify"]["secretRef"]["name"] == secret["metadata"]["name"]
    assert secret["stringData"] == {"cosign.pub": FAKE_KEY}
    assert fresh.push_target(source) == {
        "repository": "homelab-manifests",
        "tag": "break-glass",
    }


def test_the_bundle_publishes_only_the_fresh_tag_with_its_marker():
    files = fresh.bundle((fresh.FRESH,), "unused", keep_pvcs=False)
    assert "flux/fresh-drill.yaml" in files
    kinds = {
        d["kind"]
        for d in _docs({k: v for k, v in files.items() if k.startswith("cluster/")})
    }
    assert "Deployment" not in kinds
    assert any(d["metadata"]["name"] == fresh.FRESH_MARKER for d in _docs(files))


# --- The verdict ----------------------------------------------------------------------


def _items(
    *,
    url=URL,
    ready="True",
    verified="True",
    revision="break-glass@sha256:1",
    applied=None,
    marker=True,
):
    items = [
        {
            "kind": "OCIRepository",
            "metadata": {"name": "homelab", "namespace": "flux-system"},
            "spec": {"url": url},
            "status": {
                "artifact": {"revision": revision},
                "conditions": [
                    {"type": "Ready", "status": ready, "message": "m"},
                    {"type": "SourceVerified", "status": verified},
                ],
            },
        },
        {
            "kind": "Kustomization",
            "metadata": {"name": fresh.FRESH, "namespace": "flux-system"},
            "status": {"lastAppliedRevision": revision if applied is None else applied},
        },
    ]
    if marker:
        items.append(
            {
                "kind": "ConfigMap",
                "metadata": {"name": fresh.FRESH_MARKER, "namespace": fresh.FRESH},
            }
        )
    return items


def test_fresh_cluster_is_clean_on_a_verified_bundle_from_the_in_cluster_registry():
    assert fresh.fresh_cluster_problems(_items(), URL, "break-glass") == []


@pytest.mark.parametrize(
    "items",
    [
        _items(url="oci://kind-registry:5000/bg"),
        _items(ready="False"),
        _items(verified="False"),
        _items(revision="bg@sha256:1"),
        _items(applied="break-glass@sha256:0"),
        _items(marker=False),
    ],
    ids=[
        "other registry",
        "not ready",
        "not verified",
        "wrong bundle",
        "not applied",
        "no marker",
    ],
)
def test_fresh_cluster_is_flagged(items):
    assert fresh.fresh_cluster_problems(items, URL, "break-glass")
