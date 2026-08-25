#!/usr/bin/env python3
"""Tests for the k8s manifest validator's duplicate-key check.

A repeated mapping key is valid YAML — the later value silently wins — so kubectl
applies the document, every check goes green, and only the losing setting is gone.
It bit homepage's pod spec, which acquired both `automountServiceAccountToken: true`
(needed by its kubernetes widget) and a `false` from an estate-wide sweep when the
two edits met in a rebase.

Run: uv run pytest scripts/validate/test_validate_k8s_manifests.py
"""

import contextlib
import io
import re

import pytest

import validate_k8s_manifests as vkm


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


@pytest.fixture(scope="module")
def real_tree_run():
    """Run the validator over the real tree once, and hand back its exit code and stderr.

    A full run renders every k8s role and costs ~3.8s. Two guards below assert on different
    parts of the same run's stderr, and each used to pay for its own — the run is a pure
    function of the repo tree, so one serves both.

    # DECIDED: redirect_stderr, not capsys. capsys is function-scoped and pytest refuses to
    # inject it into a module-scoped fixture; main() writes with print(file=sys.stderr), which
    # redirect_stderr captures because it rebinds sys.stderr at call time.
    """
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = vkm.main()
    return rc, buf.getvalue()


def test_real_tree_has_no_unresolved_claim_name(real_tree_run):
    # The actual regression guard: every claimName across the real k8s roles must resolve
    # against a rendered PVC (or a seed-volume-backed one) — a brand-new service naming a PVC
    # that was never wired up must show here, since nothing else in the tree checks this.
    rc, err = real_tree_run
    assert rc == 0
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


# ── schema validation ────────────────────────────────────────────────────────────────────
# Every object the guard renders is checked against the upstream Kubernetes OpenAPI schema,
# which is what `kubectl apply --dry-run=server` does — offline, and covering the ~17 roles
# k8s_dry_run_unsupported refuses. These tests pin the two ways that check goes wrong: a false
# positive from PyYAML's octal parsing, and a schema version that drifts from the cluster.

VALID_DEPLOYMENT = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {"name": "example"},
    "spec": {
        "replicas": 1,
        "selector": {"matchLabels": {"app": "example"}},
        "template": {
            "metadata": {"labels": {"app": "example"}},
            "spec": {"containers": [{"name": "example", "image": "example:1"}]},
        },
    },
}


def _with_spec(**overrides) -> dict:
    doc = {**VALID_DEPLOYMENT, "spec": {**VALID_DEPLOYMENT["spec"], **overrides}}
    return doc


def test_a_valid_deployment_passes():
    assert vkm.schema_error(VALID_DEPLOYMENT) is None


def test_a_misspelled_field_is_rejected():
    # The half that strict=True buys. The API server ignores an undefined field, so a
    # `readinessProb` typo applies clean and the probe simply never runs.
    err = vkm.schema_error(_with_spec(progressDeadlineSecond=600))
    assert err is not vkm.NO_SCHEMA
    assert "progressDeadlineSecond" in err


def test_a_wrong_type_is_rejected():
    err = vkm.schema_error(_with_spec(replicas="three"))
    assert err is not vkm.NO_SCHEMA
    assert "spec.replicas" in err


def test_a_crd_reports_no_schema_rather_than_failing():
    # Traefik's IngressRoute and friends define their shape in the cluster, not in the upstream
    # spec. Reported as skipped and counted, never as a pass — 57 objects in this tree are
    # unvalidated for this reason and the run says so.
    crd = {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRoute",
        "metadata": {"name": "example"},
        "spec": {"routes": []},
    }
    assert vkm.schema_error(crd) is vkm.NO_SCHEMA


def test_octal_literals_are_read_as_kubectl_reads_them():
    # PyYAML is YAML 1.1, where `0o444` is a STRING; the parser behind kubectl reads 292.
    # Without this, four correct `defaultMode: 0o444` volumes fail as type errors. Verified
    # against live objects: scrutiny-web/scrutiny-influxdb/uptime-kuma all carry
    # secret.defaultMode: 292.
    assert vkm.normalise_octal("0o444") == 292
    assert vkm.normalise_octal({"defaultMode": "0o440"}) == {"defaultMode": 288}
    assert vkm.normalise_octal([{"m": "0o755"}]) == [{"m": 493}]


def test_octal_normalisation_leaves_other_strings_alone():
    # It must not touch an image tag, a name, or a decimal already spelled as a string.
    for value in ("0o", "0o8", "nginx:1.29", "0444", "444", ""):
        assert vkm.normalise_octal(value) == value


def test_a_defaultmode_octal_volume_passes_the_schema():
    # The end-to-end version of the two tests above, at the shape that actually bit.
    doc = _with_spec(
        template={
            "metadata": {"labels": {"app": "example"}},
            "spec": {
                "containers": [{"name": "example", "image": "example:1"}],
                "volumes": [
                    {"name": "c", "secret": {"secretName": "s", "defaultMode": "0o444"}}
                ],
            },
        }
    )
    assert vkm.schema_error(doc) is None


def test_schema_version_matches_the_cluster():
    # A cluster upgrade that leaves K8S_SCHEMA_VERSION behind validates every manifest against
    # the wrong API surface: a field added in the new minor reads as invalid, and one removed
    # in it reads as fine. Silent in both directions, hence this test.
    k3s_defaults = (
        vkm.ANSIBLE / "roles" / "setup" / "k3s" / "defaults" / "main.yml"
    ).read_text()
    match = re.search(r"^k3s_version:\s*v(\d+\.\d+)\.", k3s_defaults, re.MULTILINE)
    assert match, "k3s_version not found in roles/setup/k3s/defaults/main.yml"
    assert vkm.K8S_SCHEMA_VERSION == match.group(1), (
        f"K8S_SCHEMA_VERSION is {vkm.K8S_SCHEMA_VERSION} but the cluster runs "
        f"{match.group(1)} — bump the pin and the kubernetes-validate dependency together."
    )


def test_real_tree_passes_the_schema(real_tree_run):
    # The regression guard: no rendered object in the tree fails its schema. Paired with
    # main()'s own exit code so a new failure cannot hide behind a passing older check.
    rc, err = real_tree_run
    assert rc == 0
    assert "fails the v" not in err
