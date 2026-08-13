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
    assert "duplicate key" in error
