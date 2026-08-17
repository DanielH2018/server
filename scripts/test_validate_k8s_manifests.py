#!/usr/bin/env python3
"""Tests for the k8s manifest validator's duplicate-key check.

A repeated mapping key is valid YAML — the later value silently wins — so kubectl
applies the document, every check goes green, and only the losing setting is gone.
It bit homepage's pod spec, which acquired both `automountServiceAccountToken: true`
(needed by its kubernetes widget) and a `false` from an estate-wide sweep when the
two edits met in a rebase.

Run: uv run pytest scripts/test_validate_k8s_manifests.py
"""

import importlib.util
import os

_MOD = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "validate_k8s_manifests.py"
)
_spec = importlib.util.spec_from_file_location("validate_k8s_manifests", _MOD)
vkm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vkm)


POD_SPEC = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: homepage
spec:
  template:
    spec:
      serviceAccountName: homepage
{first}
      containers:
        - name: homepage
{second}
"""


def test_a_duplicate_key_in_one_pod_spec_is_rejected():
    rendered = POD_SPEC.format(
        first="      automountServiceAccountToken: true",
        second="      automountServiceAccountToken: false",
    )
    error = vkm.yaml_error(rendered)
    assert error is not None, "duplicate automountServiceAccountToken accepted"
    assert "duplicate key" in error


def test_the_same_key_in_two_different_mappings_is_fine():
    """The check must key off the mapping, not the document — a Deployment and its
    Service legitimately repeat `name`, and every container repeats `image`."""
    rendered = POD_SPEC.format(
        first="      automountServiceAccountToken: true",
        second="          image: homepage:latest",
    )
    assert vkm.yaml_error(rendered) is None


def test_a_duplicate_inside_an_embedded_config_blob_is_rejected():
    """ConfigMap/Secret values get the same loader — an overwritten key in Authelia's
    or Traefik's embedded config is the same silent loss, one level down."""
    rendered = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: authelia
data:
  configuration.yml: |
    session:
      name: session
      name: duplicate
"""
    error = vkm.yaml_error(rendered)
    assert error is not None, "duplicate key in embedded YAML accepted"


# --------------------------------------------------------------------- PVC claimName cross-check

DEPLOYMENT_WITH_CLAIM = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: example
spec:
  template:
    spec:
      containers:
        - name: example
          image: example:latest
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: {claim}
"""

PVC_DOC = """\
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {name}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
"""


def test_find_pvc_names_reads_a_pvc_object():
    doc = list(vkm.parse_docs(PVC_DOC.format(name="example-data")))[0]
    assert vkm.find_pvc_names(doc) == ["example-data"]


def test_find_pvc_names_ignores_a_non_pvc_object():
    doc = list(vkm.parse_docs(DEPLOYMENT_WITH_CLAIM.format(claim="x")))[0]
    assert vkm.find_pvc_names(doc) == []


def test_find_claim_name_refs_finds_a_deployment_volume():
    doc = list(vkm.parse_docs(DEPLOYMENT_WITH_CLAIM.format(claim="example-data")))[0]
    assert vkm.find_claim_name_refs(doc) == ["example-data"]


def test_find_claim_name_refs_finds_a_cronjob_nested_one_level_deeper():
    rendered = """\
apiVersion: batch/v1
kind: CronJob
metadata:
  name: example
spec:
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: example
              image: example:latest
          volumes:
            - name: data
              persistentVolumeClaim:
                claimName: example-data
"""
    doc = list(vkm.parse_docs(rendered))[0]
    assert vkm.find_claim_name_refs(doc) == ["example-data"]


def test_find_claim_name_refs_finds_multiple_across_a_document():
    rendered = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: example
spec:
  template:
    spec:
      containers:
        - name: example
          image: example:latest
      volumes:
        - name: a
          persistentVolumeClaim:
            claimName: claim-a
        - name: b
          persistentVolumeClaim:
            claimName: claim-b
"""
    doc = list(vkm.parse_docs(rendered))[0]
    assert sorted(vkm.find_claim_name_refs(doc)) == ["claim-a", "claim-b"]


def test_real_tree_has_no_unresolved_claim_name(capsys):
    # The actual regression guard: every claimName across the real k8s roles must resolve
    # against a rendered PVC (or a seed-volume-backed one) — a brand-new service naming a PVC
    # that was never wired up must show here, since nothing else in the tree checks this.
    assert vkm.main() == 0
    err = capsys.readouterr().err
    assert "matches no rendered PersistentVolumeClaim" not in err


def test_seed_volume_pvc_names_resolves_a_real_seed_backed_role():
    # tdarr's config PVC is created by seed-volume's own pvc.yaml.j2 (never rendered under
    # seed-volume's own role — it's in SKIP_ROLES), using vars tdarr's include_role task passes.
    # tdarr's deployment.yaml.j2 references the SAME value directly as a claimName, so without
    # this resolving, tdarr's config claim would show as unresolved on every real run.
    base = {
        **vkm.BASE_CONTEXT,
        **vkm.load_yaml(vkm.ALL_VARS),
        "playbook_dir": str(vkm.ANSIBLE),
    }
    base = vkm.resolve_vars(base, base)
    ctx = {**base, **vkm.role_defaults("tdarr", base)}
    names = vkm.seed_volume_pvc_names("tdarr", ctx)
    assert ctx["tdarr_k8s_configs_claim"] in names
