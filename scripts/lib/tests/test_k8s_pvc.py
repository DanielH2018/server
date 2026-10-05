#!/usr/bin/env python3
"""PVC names a rendered manifest declares, and the `claimName`s it references.

A Deployment mounting a PVC nothing declares passes admission — PVC binding is a scheduling
concern, not a validating webhook — so the validator cross-references the two sets across the
whole tree. These cover the two halves of that index.

Run: uv run pytest scripts/lib/tests/test_k8s_pvc.py
"""

from lib.k8s_pvc import (
    find_claim_name_refs,
    find_pvc_names,
    parse_docs,
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
