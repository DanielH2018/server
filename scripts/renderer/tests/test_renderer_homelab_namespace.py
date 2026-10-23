"""The shared kind `homelab-namespace` and the edge every `homelab` tag takes to it (D5 step 30).

The kind renders the `homelab` Namespace with the prune exemption `prune_label.py` stamps on a
flux-owned tag's Namespace, and the LimitRange from `k8s_default_limitrange`. A tag with a
document in `k8s_namespace`, in any of its directories, depends on the kind, and the tag's
pre-apply kinds copy the edge.

Run: uv run pytest scripts/renderer/tests/test_renderer_homelab_namespace.py
"""

from lib import yaml_fast
from renderer.renderer_lib import cluster as cluster_mod
from renderer.renderer_lib.bundle import bundle_files, homelab_namespace_files
from renderer.renderer_lib.namespace import HOMELAB_NAMESPACE
from renderer.renderer_lib.ordering import Ordering
from renderer.renderer_lib.prune_label import label

TIMEOUT = "600s"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
CONFIGMAP = "kind: ConfigMap\nmetadata:\n  name: x\n  namespace: {namespace}\n"
HOMELAB = Ordering(flux_owned=None, workload_namespace="homelab")


def _depends(ordering: Ordering, tags: list[str], directories: dict) -> dict:
    """Each Kustomization `bundle_files` writes for `tags`, mapped to its `dependsOn` names."""
    files = bundle_files(directories, {}, tags, TIMEOUT, DOTENV, (), ordering)
    return {
        doc["metadata"]["name"]: [d["name"] for d in doc["spec"].get("dependsOn", [])]
        for tag in tags
        for doc in yaml_fast.safe_load_all(files[f"flux/{tag}.yaml"])
    }


def _in(namespace: str) -> dict[str, str]:
    return {"x.yaml": CONFIGMAP.format(namespace=namespace)}


DIRECTORIES = {
    "sonarr": _in("homelab"),
    "deferred-only": _in("observability"),
    "deferred-only-deferred": _in("homelab"),
    "pihole": _in("homelab"),
    "pihole-precheck": _in("homelab"),
    "elsewhere": _in("kube-system"),
}
TAGS = ["deferred-only", "elsewhere", "pihole", "sonarr"]


def test_a_tag_rendering_into_homelab_depends_on_the_kind():
    depends = _depends(HOMELAB, TAGS, DIRECTORIES)
    assert HOMELAB_NAMESPACE in depends["sonarr"]
    # Its only `homelab` document is deferred, and `<tag>` still takes the edge.
    assert HOMELAB_NAMESPACE in depends["deferred-only"]
    # A pre-apply kind runs before `<tag>`, so it copies the edge.
    assert HOMELAB_NAMESPACE in depends["pihole-precheck"]
    assert HOMELAB_NAMESPACE not in depends["elsewhere"]


def test_an_ordering_without_the_namespace_generates_no_edge():
    depends = _depends(Ordering(), TAGS, DIRECTORIES)
    assert all(HOMELAB_NAMESPACE not in names for names in depends.values())


def test_the_bundle_ordering_names_k8s_namespace():
    assert cluster_mod.bundle_ordering(set()).workload_namespace == "homelab"


def test_the_namespace_carries_the_prune_exemption_prune_label_stamps():
    text = homelab_namespace_files(TIMEOUT)[
        f"cluster/{HOMELAB_NAMESPACE}/namespace.yaml"
    ]
    assert "kustomize.toolkit.fluxcd.io/prune: disabled" in text
    assert label(text, "homelab-namespace/namespace.yaml") == text


def test_the_kustomization_orphans_its_objects_and_waits_on_nothing():
    files = homelab_namespace_files(TIMEOUT)
    (doc,) = yaml_fast.safe_load_all(files[f"flux/{HOMELAB_NAMESPACE}.yaml"])
    assert doc["spec"]["deletionPolicy"] == "Orphan"
    assert doc["spec"]["path"] == f"./cluster/{HOMELAB_NAMESPACE}"
    assert "dependsOn" not in doc["spec"]
