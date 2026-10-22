"""The bootstrap drill's fixture bundle and its three verdicts (D5 step 28).

Every verdict has a green and a red input, so a verdict that stopped matching fails here.
`sops` is replaced by a stub: the encryption itself is the workflow's to prove.

Run: uv run pytest scripts/deploy_tools/tests/test_bootstrap_drill.py
"""

import copy

import pytest
from lib import yaml_fast

import bootstrap_drill as drill

SYNC_DOCS = list(
    yaml_fast.safe_load_all(
        """
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata: {name: homelab, namespace: flux-system}
spec:
  url: oci://ghcr.io/example/manifests
  ref: {tag: prod}
  verify: {provider: cosign}
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {name: homelab, namespace: flux-system}
spec: {path: ./flux, prune: true}
"""
    )
)
UID = "uid-1"


def _obj(kind, name, namespace, meta=None, **extra):
    metadata = {"name": name, "namespace": namespace, **(meta or {})}
    return {"kind": kind, "metadata": metadata, **extra}


def _ks(name, ready="True", labels=None, message=""):
    cond = {"type": "Ready", "status": ready, "message": message}
    return _obj(
        "Kustomization",
        name,
        "flux-system",
        {"labels": labels or {}},
        status={"conditions": [cond]},
    )


def _claim(labels, uid=UID):
    meta = {"labels": labels, "uid": uid}
    return _obj("PersistentVolumeClaim", drill.CLAIM, drill.NAMESPACE, meta)


OWNED = {drill.MARKER_LABEL: "true", drill.OWNER_LABEL: drill.STATEFUL}
BEFORE = [_claim({drill.MARKER_LABEL: "true"})]
AFTER = [
    _ks(drill.STATEFUL),
    _ks(drill.PLUMBING, labels={drill.OWNER_LABEL: "homelab"}),
    _obj("ConfigMap", drill.MARKER_CONFIGMAP, drill.NAMESPACE),
    _claim(OWNED),
    _obj(
        "Secret",
        drill.CLUSTER_VARS_SECRET,
        "flux-system",
        data={drill.FIXTURE_KEY: "b24="},
    ),
]


def test_before_root_is_clean_when_only_the_standin_wrote_the_claim():
    assert drill.before_root_problems(BEFORE) == []


def test_before_root_flags_a_claim_the_root_created_first():
    problems = drill.before_root_problems([_claim(OWNED), _ks(drill.STATEFUL)])
    assert any("already carries" in p for p in problems)
    assert any("exists before the root" in p for p in problems)


def test_after_root_is_clean_when_the_root_adopted_the_standins_claim():
    assert drill.after_root_problems(AFTER, UID) == []


def _without(kind):
    return [x for x in AFTER if x["kind"] != kind]


def _stateful_not_ready():
    return [_ks(drill.STATEFUL, "False") if x["metadata"]["name"] == drill.STATEFUL else x
            for x in AFTER]  # fmt: skip


@pytest.mark.parametrize(
    ("items", "expect"),
    [
        (_without("ConfigMap"), "ConfigMap"),
        (_without("Secret"), "holds no"),
        (_stateful_not_ready(), "not Ready"),
    ],
)
def test_after_root_flags_each_missing_result(items, expect):
    assert any(expect in p for p in drill.after_root_problems(items, UID))


def test_after_root_flags_a_recreated_claim():
    assert any(
        "uid changed" in p for p in drill.after_root_problems(AFTER, "other-uid")
    )


def test_no_decrypt_passes_only_on_a_decryption_failure():
    failed = [
        _ks("cluster-vars", "False", message="failed to decrypt: sops-age not found")
    ]
    assert drill.no_decrypt_problems(failed) == []
    assert drill.no_decrypt_problems([_ks("cluster-vars")])
    assert drill.no_decrypt_problems([_ks("cluster-vars", "False", message="timeout")])


def test_closure_reaching_flux_deferred_or_the_root_is_flagged():
    clean = {"a": ["cluster-vars"], "cluster-vars": []}
    assert drill.closure_problems(clean, {"a"}) == []
    assert drill.closure_problems({"a": ["b"], "b": ["homelab"]}, {"a"})
    assert drill.closure_problems({"a": ["flux-deferred"]}, {"a"})


def test_placeholders_in_a_hand_applied_document_are_flagged():
    assert drill.placeholder_problems(SYNC_DOCS) == []
    bad = copy.deepcopy(SYNC_DOCS)
    bad[0]["spec"]["url"] = "oci://${registry}/manifests"
    assert drill.placeholder_problems(bad)
    bad[0]["spec"]["url"] = "oci://$$registry/manifests"
    assert drill.placeholder_problems(bad)


def test_sync_problems_name_a_missing_root():
    assert drill.sync_problems(SYNC_DOCS) == []
    assert drill.sync_problems(SYNC_DOCS[:1]) == [
        "flux-deferred holds no Kustomization homelab"
    ]


def test_build_writes_the_withheld_root_order(tmp_path):
    drill.build(tmp_path, SYNC_DOCS, "reg:5000", lambda text: "ENC:" + text)
    hand = tmp_path / "hand"
    source = yaml_fast.safe_load((hand / "ocirepository.yaml").read_text())
    assert "verify" not in source["spec"]
    assert source["spec"]["url"] == "oci://reg:5000/drill"
    assert source["spec"]["insecure"] is True
    root = yaml_fast.safe_load((hand / "root.yaml").read_text())
    assert root["metadata"]["name"] == "homelab"
    assert "suspend" not in root["spec"]
    step6 = [p.rsplit("/", 1)[1] for p in (hand / "step6.txt").read_text().split()]
    assert step6 == ["cluster-vars.yaml", "drill-plumbing.yaml"]
    standin = yaml_fast.safe_load((hand / "standin-claim.yaml").read_text())
    assert standin["metadata"]["labels"] == {drill.MARKER_LABEL: "true"}
    # The stateful tag is in the root's list but never in the hand-applied closure.
    assert (
        "drill-stateful.yaml"
        in (tmp_path / "bundle/flux/kustomization.yaml").read_text()
    )
