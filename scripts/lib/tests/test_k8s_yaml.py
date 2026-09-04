#!/usr/bin/env python3
"""A repeated mapping key must be rejected, in the manifest and in what it embeds.

Valid YAML lets the later value silently win — so kubectl applies the document, every check
goes green, and only the losing setting is gone. It bit homepage's pod spec, which acquired
both `automountServiceAccountToken: true` (needed by its kubernetes widget) and a `false` from
an estate-wide sweep when the two edits met in a rebase.

Split out of scripts/validate/tests/test_validate_k8s_manifests.py on 2026-09-04, with the
code it covers.

Run: uv run pytest scripts/lib/tests/test_k8s_yaml.py
"""

from lib.k8s_yaml import yaml_error


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
    error = yaml_error(rendered)
    assert error is not None, "duplicate automountServiceAccountToken accepted"
    assert "duplicate key" in error


def test_the_same_key_in_two_different_mappings_is_fine():
    """The check must key off the mapping, not the document — a Deployment and its
    Service legitimately repeat `name`, and every container repeats `image`."""
    rendered = POD_SPEC.format(
        first="      automountServiceAccountToken: true",
        second="          image: homepage:latest",
    )
    assert yaml_error(rendered) is None


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
    error = yaml_error(rendered)
    assert error is not None, "duplicate key in embedded YAML accepted"
