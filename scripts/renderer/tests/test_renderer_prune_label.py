"""D5.1a2's prune exemption: every claim and Namespace a flux-owned tag renders carries the label.

The fixtures are rendered texts in the three shapes the tree produces: a claim with no
`labels`, a claim a prune-armed role's `homelab_role_label` re-serialised with a `labels` map,
and observability's Namespace with its Pod Security labels. The live test renders two real
roles as if flux-owned, through the CLI, with a fixture cluster file.

Run: uv run pytest scripts/renderer/tests/test_renderer_prune_label.py
"""

import pytest

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.cluster import RenderedTag
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.prune_label import (
    EXEMPT_KINDS,
    PRUNE_DISABLED,
    PRUNE_LABEL,
    label,
    label_tags,
)

LINE = f"{PRUNE_LABEL}: {PRUNE_DISABLED}"
CLAIM = (
    "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: data\n"
    "  namespace: homelab\nspec:\n  storageClassName: longhorn\n"
)
ARMED_CLAIM = (
    "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: data\n"
    "  labels:\n    homelab/role: sonarr\nspec:\n  storageClassName: longhorn\n"
)
NAMESPACE = (
    "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: observability\n"
    "  labels:\n    pod-security.kubernetes.io/warn: baseline\n"
)
SERVICE = 'apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n  annotations:\n    x: "${key}"\n'
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
# observability renders the only Namespace and five claims; sonarr one claim through a
# prune-armed role's re-serialised labels.
EXPECTED_EXEMPT = frozenset(
    {("Namespace", "observability"), ("PersistentVolumeClaim", "sonarr-config")}
)


def _labels(text: str) -> list[dict]:
    return [
        (d.get("metadata") or {}).get("labels") or {}
        for d in yaml_fast.safe_load_all(text)
        if d
    ]


def test_a_claim_without_labels_gains_a_labels_map_and_no_other_byte_moves():
    out = label(CLAIM, "t/claim-data.yaml")
    assert _labels(out) == [{PRUNE_LABEL: PRUNE_DISABLED}]
    assert out == CLAIM.replace(
        "  name: data\n", f"  labels:\n    {LINE}\n  name: data\n"
    )


def test_a_claim_with_the_role_label_keeps_it_beside_the_prune_label():
    out = label(ARMED_CLAIM, "t/claim-data.yaml")
    assert _labels(out) == [{PRUNE_LABEL: PRUNE_DISABLED, "homelab/role": "sonarr"}]


def test_a_namespace_keeps_its_own_labels_beside_the_prune_label():
    out = label(NAMESPACE, "t/00-namespace.yaml")
    assert _labels(out)[0][PRUNE_LABEL] == PRUNE_DISABLED
    assert _labels(out)[0]["pod-security.kubernetes.io/warn"] == "baseline"


def test_another_kind_is_left_byte_for_byte():
    text = f"{SERVICE}---\n{CLAIM}"
    out = label(text, "t/f.yaml")
    assert out.startswith(SERVICE)
    assert _labels(out) == [{}, {PRUNE_LABEL: PRUNE_DISABLED}]


def test_a_document_already_labelled_disabled_is_left_alone():
    once = label(CLAIM, "t/f.yaml")
    assert label(once, "t/f.yaml") == once


def test_a_document_labelled_with_another_value_is_flagged():
    text = ARMED_CLAIM.replace("homelab/role: sonarr", f"{PRUNE_LABEL}: enabled")
    with pytest.raises(RenderError, match="other than disabled"):
        label(text, "t/f.yaml")


def test_only_a_flux_owned_tags_directories_are_labelled():
    owned = RenderedTag("a", {"claim.yaml": CLAIM}, {"ns.yaml": NAMESPACE})
    other = RenderedTag("b", {"claim.yaml": CLAIM})
    a, b = label_tags([owned, other], {"a"})
    assert _labels(a.files["claim.yaml"])[0][PRUNE_LABEL] == PRUNE_DISABLED
    assert _labels(a.deferred["ns.yaml"])[0][PRUNE_LABEL] == PRUNE_DISABLED
    assert b == other


def test_every_claim_and_namespace_of_a_live_flux_owned_render_carries_the_label(
    tmp_path,
):
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    tags = ["observability", "sonarr"]
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out, tags, None, flux_owned=lambda: set(tags), cluster_file=cluster_file
    )
    assert code == RENDER_OK
    exempt = {}
    for path in sorted((out / "cluster").glob("*/*.yaml")):
        for doc in yaml_fast.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind") in EXEMPT_KINDS:
                meta = doc["metadata"]
                exempt[(doc["kind"], meta["name"])] = (meta.get("labels") or {}).get(
                    PRUNE_LABEL
                )
    assert EXPECTED_EXEMPT <= exempt.keys()
    assert {k: v for k, v in exempt.items() if v != PRUNE_DISABLED} == {}
