#!/usr/bin/env python3
"""A repeated mapping key must be rejected, in the manifest and in what it embeds.

Valid YAML lets the later value silently win — so kubectl applies the document, every check
goes green, and only the losing setting is gone. It bit homepage's pod spec, which acquired
both `automountServiceAccountToken: true` (needed by its kubernetes widget) and a `false` from
an estate-wide sweep when the two edits met in a rebase.

The PVC half covers the names a rendered manifest declares and the `claimName`s it references.
A Deployment mounting a PVC nothing declares passes admission — PVC binding is a scheduling
concern, not a validating webhook — so the validator cross-references the two sets across the
whole tree.

Run: uv run pytest scripts/lib/tests/test_k8s_yaml.py
"""

from _image_builder_tag import templar_value
from lib.ansible_jinja_env import make_ansible_env
from lib.k8s_yaml import (
    find_claim_name_refs,
    find_pvc_names,
    make_lookup,
    parse_docs,
    yaml_error,
)


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


def _guard_lookup(expression: str) -> str:
    env = make_ansible_env()
    env.globals["lookup"] = make_lookup({})
    return env.from_string(expression).render()


def test_a_template_lookup_keeps_trailing_newlines_as_ansible_does(tmp_path):
    """A guard reading fewer newlines than a deploy flips a `|` block scalar to `|-`."""
    tpl = tmp_path / "templates" / "tail.j2"
    tpl.parent.mkdir()
    tpl.write_text("x\n\n")
    expression = f"{{{{ lookup('template', '{tpl}') }}}}"
    assert _guard_lookup(expression) == "x\n\n"
    assert _guard_lookup(expression) == templar_value(expression, {}, tmp_path)


def test_a_file_lookup_strips_all_trailing_whitespace_as_ansible_does(tmp_path):
    src = tmp_path / "tail.txt"
    src.write_text("x \t\n\n")
    expression = f"{{{{ lookup('file', '{src}') }}}}"
    assert _guard_lookup(expression) == "x"
    assert _guard_lookup(expression) == templar_value(expression, {}, tmp_path)
