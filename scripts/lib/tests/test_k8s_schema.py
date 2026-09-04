#!/usr/bin/env python3
"""Schema validation of a rendered object: the core OpenAPI check and the vendored CRD schemas.

Two ways the check goes wrong are pinned here: a false positive from PyYAML's octal parsing,
and a schema version that drifts from the cluster. The CRD half covers what the vendored
catalog schemas do catch (a misspelled key, a missing required field) and, deliberately, what
they do not.

Split out of scripts/validate/tests/test_validate_k8s_manifests.py on 2026-09-04, with the
code it covers.

Run: uv run pytest scripts/lib/tests/test_k8s_schema.py
"""

import re

from typing import Any

from lib.k8s_net_rules import https_route_without_tls
from lib.k8s_schema import (
    K8S_SCHEMA_VERSION,
    NO_SCHEMA,
    crd_schema_error,
    crd_schema_path,
    normalise_octal,
    schema_error,
)
from lib.repo_paths import ANSIBLE


# ── schema validation ────────────────────────────────────────────────────────────────────
# Every object the guard renders is checked against the upstream Kubernetes OpenAPI schema,
# which is what `kubectl apply --dry-run=server` does — offline, and covering the ~17 roles
# k8s_dry_run_unsupported refuses. These tests pin the two ways that check goes wrong: a false
# positive from PyYAML's octal parsing, and a schema version that drifts from the cluster.

VALID_DEPLOYMENT: dict[str, Any] = {
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
    assert schema_error(VALID_DEPLOYMENT) is None


def test_a_misspelled_field_is_rejected():
    # The half that strict=True buys. The API server ignores an undefined field, so a
    # `readinessProb` typo applies clean and the probe simply never runs.
    err = schema_error(_with_spec(progressDeadlineSecond=600))
    assert err is not NO_SCHEMA
    assert "progressDeadlineSecond" in err


def test_a_wrong_type_is_rejected():
    err = schema_error(_with_spec(replicas="three"))
    assert err is not NO_SCHEMA
    assert "spec.replicas" in err


def test_a_crd_falls_through_to_its_vendored_schema():
    # Traefik's IngressRoute and friends define their shape in the cluster, not in the upstream
    # spec, so kubernetes_validate raises SchemaNotFoundError for every one. This used to be
    # reported as skipped and counted — honest, but 60 objects went unvalidated. They now fall
    # through to the vendored catalog schema, so a well-formed one PASSES rather than skips.
    crd = {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRoute",
        "metadata": {"name": "example"},
        "spec": {"routes": []},
    }
    assert schema_error(crd) is None


def test_a_crd_with_no_vendored_schema_still_reports_no_schema():
    # The skip path is not gone, only narrowed: a CRD group nothing has vendored is still
    # counted and named rather than silently passed.
    crd = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": "example"},
        "spec": {},
    }
    assert schema_error(crd) is NO_SCHEMA


def test_octal_literals_are_read_as_kubectl_reads_them():
    # PyYAML is YAML 1.1, where `0o444` is a STRING; the parser behind kubectl reads 292.
    # Without this, four correct `defaultMode: 0o444` volumes fail as type errors. Verified
    # against live objects: scrutiny-web/scrutiny-influxdb/uptime-kuma all carry
    # secret.defaultMode: 292.
    assert normalise_octal("0o444") == 292
    assert normalise_octal({"defaultMode": "0o440"}) == {"defaultMode": 288}
    assert normalise_octal([{"m": "0o755"}]) == [{"m": 493}]


def test_octal_normalisation_leaves_other_strings_alone():
    # It must not touch an image tag, a name, or a decimal already spelled as a string.
    for value in ("0o", "0o8", "nginx:1.29", "0444", "444", ""):
        assert normalise_octal(value) == value


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
    assert schema_error(doc) is None


def test_schema_version_matches_the_cluster():
    # A cluster upgrade that leaves K8S_SCHEMA_VERSION behind validates every manifest against
    # the wrong API surface: a field added in the new minor reads as invalid, and one removed
    # in it reads as fine. Silent in both directions, hence this test.
    k3s_defaults = (
        ANSIBLE / "roles" / "setup" / "k3s" / "defaults" / "main.yml"
    ).read_text()
    match = re.search(r"^k3s_version:\s*v(\d+\.\d+)\.", k3s_defaults, re.MULTILINE)
    assert match, "k3s_version not found in roles/setup/k3s/defaults/main.yml"
    assert K8S_SCHEMA_VERSION == match.group(1), (
        f"K8S_SCHEMA_VERSION is {K8S_SCHEMA_VERSION} but the cluster runs "
        f"{match.group(1)} — bump the pin and the kubernetes-validate dependency together."
    )


# ── CRD schema validation ───────────────────────────────────────────────────────────────────
# kubernetes_validate has no schema for a CRD — a CRD's schema lives in the cluster, not in the
# upstream OpenAPI spec — so every Traefik object in this tree used to be counted as skipped and
# checked by nothing: 46 IngressRoute, 11 Middleware, 3 TLSOption. They now validate against the
# schemas vendored under scripts/validate/schemas/.


def _ingressroute(spec):
    return {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRoute",
        "metadata": {"name": "r"},
        "spec": spec,
    }


_ROUTE = {
    "match": "Host(`x.example.com`)",
    "kind": "Rule",
    "services": [{"name": "svc", "port": 80}],
}


def test_crd_schema_path_follows_the_catalog_layout():
    path = crd_schema_path(_ingressroute({"routes": [_ROUTE]}))
    assert path is not None
    assert path.parent.name == "traefik.io"
    assert path.name == "ingressroute_v1alpha1.json"


def test_a_core_object_has_no_crd_schema_path():
    # apiVersion "v1" carries no group, so there is nothing to look up — kubernetes_validate
    # owns core objects and must not be shadowed by a vendored file.
    assert crd_schema_path({"apiVersion": "v1", "kind": "Service"}) is None


def test_a_valid_ingressroute_is_clean():
    assert crd_schema_error(_ingressroute({"routes": [_ROUTE]})) is None


def test_a_misspelled_spec_key_is_flagged():
    # `entrypoints` for `entryPoints`. The API server ignores an unknown field, so the object
    # applies clean and the route simply never binds to the entrypoint — the silent class.
    err = crd_schema_error(
        _ingressroute({"entrypoints": ["https"], "routes": [_ROUTE]})
    )
    assert isinstance(err, str) and "entrypoints" in err


def test_a_misspelled_route_key_is_flagged():
    # `middleware` for `middlewares`, one level deeper than the spec.
    route = dict(_ROUTE, middleware=[{"name": "m"}])
    err = crd_schema_error(_ingressroute({"routes": [route]}))
    assert isinstance(err, str) and "middleware" in err


def test_a_route_without_match_is_flagged():
    err = crd_schema_error(_ingressroute({"routes": [{"kind": "Rule"}]}))
    assert isinstance(err, str) and "match" in err


def test_an_unknown_crd_kind_reports_no_schema():
    unknown = {"apiVersion": "example.com/v1", "kind": "Widget", "spec": {}}
    assert crd_schema_error(unknown) is NO_SCHEMA


def test_the_schema_does_not_catch_a_missing_tls_block():
    """The limit of structural validation, asserted so nobody claims coverage it lacks.

    `tls` is optional in the IngressRoute CRD — plain-HTTP routes are legal — so an https route
    with no `spec.tls` is a valid document and passes here, while silently never matching.
    `https_route_without_tls` is what catches that, and this test fails if someone ever removes
    it believing the schema had taken over.
    """
    doc = _ingressroute({"entryPoints": ["https"], "routes": [_ROUTE]})
    assert crd_schema_error(doc) is None
    assert https_route_without_tls(doc) is not None
