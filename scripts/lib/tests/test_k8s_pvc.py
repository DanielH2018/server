#!/usr/bin/env python3
"""PVC names a rendered manifest declares, and the `claimName`s it references.

A Deployment mounting a PVC nothing declares passes admission — PVC binding is a scheduling
concern, not a validating webhook — so the validator cross-references the two sets across the
whole tree. These cover the two halves of that index, plus the volume-claim include the
declaring half would otherwise miss.

Split out of scripts/validate/tests/test_validate_k8s_manifests.py on 2026-09-04, with the
code it covers.

Run: uv run pytest scripts/lib/tests/test_k8s_pvc.py
"""

from lib.k8s_context import resolve_vars, role_defaults
from lib.k8s_pvc import (
    find_claim_name_refs,
    find_pvc_names,
    parse_docs,
    volume_claim_pvc_names,
)
from lib.render_guard import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    SHARED_TPL,
    load_yaml,
    make_env,
)


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
    doc = list(parse_docs(PVC_DOC.format(name="example-data")))[0]
    assert find_pvc_names(doc) == ["example-data"]


def test_find_pvc_names_ignores_a_non_pvc_object():
    doc = list(parse_docs(DEPLOYMENT_WITH_CLAIM.format(claim="x")))[0]
    assert find_pvc_names(doc) == []


def test_find_claim_name_refs_finds_a_deployment_volume():
    doc = list(parse_docs(DEPLOYMENT_WITH_CLAIM.format(claim="example-data")))[0]
    assert find_claim_name_refs(doc) == ["example-data"]


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
    doc = list(parse_docs(rendered))[0]
    assert find_claim_name_refs(doc) == ["example-data"]


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
    doc = list(parse_docs(rendered))[0]
    assert sorted(find_claim_name_refs(doc)) == ["claim-a", "claim-b"]


def test_volume_claim_pvc_names_resolves_a_real_claim_backed_role():
    # tdarr's config PVC is created by volume-claim's own pvc.yaml.j2 (never rendered under
    # volume-claim's own role — it's in SKIP_ROLES), using vars tdarr's include_role task passes.
    # tdarr's deployment.yaml.j2 references the SAME value directly as a claimName, so without
    # this resolving, tdarr's config claim would show as unresolved on every real run.
    base = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        "playbook_dir": str(ANSIBLE),
    }
    base = resolve_vars(base, base)
    ctx = {**base, **role_defaults("tdarr", base)}
    names = volume_claim_pvc_names("tdarr", ctx, make_env([SHARED_TPL]))
    assert ctx["tdarr_k8s_configs_claim"] in names
