#!/usr/bin/env python3
"""Schema validation of a rendered object: the core OpenAPI check and the vendored CRD schemas.

Two ways the check goes wrong are pinned here: a false positive from PyYAML's octal parsing,
and a schema version that drifts from the cluster. The CRD half covers what the vendored
catalog schemas do catch (a misspelled key, a missing required field) and, deliberately, what
they do not.

Run: uv run pytest scripts/validate/tests/test_k8s_schema.py
"""

import json

from typing import Any

from validate.validate_lib.k8s_net_rules import https_route_without_tls
from validate.refresh_vendored_schemas import kubernetes_tag
from validate.validate_lib.k8s_schema import (
    CORE_SCHEMA_DIR,
    K8S_SCHEMA_VERSION,
    NO_SCHEMA,
    crd_schema_error,
    crd_schema_path,
    normalise_octal,
    schema_error,
    strict_schema,
)
from lib.repo_paths import K3S_DEFAULTS, REPO


# ── schema validation ────────────────────────────────────────────────────────────────────
# Every object the guard renders is checked against the upstream Kubernetes OpenAPI schema,
# which is what `kubectl apply --dry-run=server` does, offline. These tests pin the two ways
# that check goes wrong: a false positive from PyYAML's octal parsing, and a schema version
# that drifts from the cluster.

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
    assert isinstance(err, str)
    assert "progressDeadlineSecond" in err


def test_a_wrong_type_is_rejected():
    err = schema_error(_with_spec(replicas="three"))
    assert isinstance(err, str)
    assert "spec.replicas" in err


def test_a_misspelled_field_deep_in_a_pod_spec_is_rejected():
    # Strictness has to reach the nested components, not just the Deployment's own spec: the
    # probe typo the module docstring names lives three $refs down.
    container = {"name": "example", "image": "example:1", "readinessProb": {}}
    template = {
        "metadata": {"labels": {"app": "example"}},
        "spec": {"containers": [container]},
    }
    err = schema_error(_with_spec(template=template))
    assert isinstance(err, str)
    assert "readinessProb" in err


def test_a_free_form_map_accepts_any_key():
    # ConfigMap data and labels are maps, not objects with properties, so strictness must leave
    # them open: any key a role writes there is legal.
    doc = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "c", "labels": {"anything.example/goes": "here"}},
        "data": {"some-file.yaml": "a: 1", "another_key": "x"},
    }
    assert schema_error(doc) is None


def test_null_in_a_required_field_is_flagged():
    # The API server treats null as omitted, and omitting a required field is an error, so a
    # required property keeps its type un-nullable: a container with `name: null` is rejected.
    template = {
        "metadata": {"labels": {"app": "example"}},
        "spec": {"containers": [{"name": None, "image": "example:1"}]},
    }
    err = schema_error(_with_spec(template=template))
    assert err is not NO_SCHEMA and err is not None


def test_null_in_an_optional_field_is_clean():
    assert schema_error(_with_spec(minReadySeconds=None)) is None


def test_strict_schema_closes_objects_with_properties_and_only_those():
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    }
    strict = strict_schema(schema)
    assert strict["additionalProperties"] is False
    assert strict["type"] == "object"
    assert strict["properties"]["a"]["type"] == "string"
    assert strict["properties"]["b"]["type"] == ["integer", "null"]
    open_map = strict_schema(
        {"type": "object", "additionalProperties": {"type": "string"}}
    )
    assert (
        "additionalProperties" in open_map
        and open_map["additionalProperties"] is not False
    )


def test_a_crd_falls_through_to_its_vendored_schema():
    # Traefik's IngressRoute and friends define their shape in the cluster, not in the upstream
    # spec, so no vendored core file describes them. They fall through to the vendored catalog
    # schema, so a well-formed one PASSES rather than skips.
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


def _vendored_tag() -> str:
    return json.loads((CORE_SCHEMA_DIR / "SOURCE.json").read_text())["tag"]


def schema_tag_problem(
    k3s_defaults: str, vendored_tag: str, schema_version: str
) -> str | None:
    """Why the vendored core schemas do not describe the cluster k3s_version pins, or None."""
    tag = kubernetes_tag(k3s_defaults)
    if vendored_tag != tag:
        return (
            f"the vendored core schemas are from {vendored_tag} but k3s_version builds on {tag} — "
            "run `uv run python scripts/validate/refresh_vendored_schemas.py` and commit the result."
        )
    if f"v{schema_version}" != ".".join(tag.split(".")[:2]):
        return (
            f"K8S_SCHEMA_VERSION is {schema_version} but k3s_version builds on {tag}."
        )
    return None


def test_vendored_core_schemas_match_the_cluster():
    # A cluster upgrade that leaves the schemas behind validates every manifest against the
    # wrong API surface: a field added in the new minor reads as invalid, and one removed in it
    # reads as fine. Silent in both directions, hence this test. It compares the FULL tag, so a
    # patch bump refreshes the schemas too and the vendored files always name the release the
    # cluster runs.
    k3s_defaults = K3S_DEFAULTS.read_text()
    assert schema_tag_problem(k3s_defaults, _vendored_tag(), K8S_SCHEMA_VERSION) is None


def test_matching_pins_and_schemas_are_clean():
    assert schema_tag_problem("k3s_version: v1.36.4+k3s1\n", "v1.36.4", "1.36") is None


def test_a_patch_bump_without_a_refresh_is_flagged():
    problem = schema_tag_problem("k3s_version: v1.36.5+k3s1\n", "v1.36.4", "1.36")
    assert problem is not None and "refresh_vendored_schemas.py" in problem


def test_a_minor_bump_without_the_schema_version_is_flagged():
    problem = schema_tag_problem("k3s_version: v1.37.0+k3s1\n", "v1.37.0", "1.36")
    assert problem is not None and "K8S_SCHEMA_VERSION" in problem


def test_only_the_current_minor_is_vendored():
    # A minor refresh writes a new v<minor>/ directory and leaves the old one, which nothing
    # reads any more. Dead schemas would still be reviewed, refreshed and shipped.
    vendored = sorted(p.name for p in CORE_SCHEMA_DIR.parent.iterdir() if p.is_dir())
    assert vendored == [f"v{K8S_SCHEMA_VERSION}"], (
        f"schemas/kubernetes.io holds {vendored}; delete every directory but "
        f"v{K8S_SCHEMA_VERSION}."
    )


def _renovate_k3s_cap() -> str | None:
    rules = json.loads((REPO / "renovate.json").read_text())["packageRules"]
    for rule in rules:
        if (
            rule.get("matchPackageNames") == ["k3s-io/k3s"]
            and "allowedVersions" in rule
        ):
            return rule["allowedVersions"]
    return None


def _cap_for(schema_version: str) -> str:
    major, minor = schema_version.split(".")
    return f"<{major}.{int(minor) + 1}"


def test_renovate_k3s_cap_follows_the_schema():
    # Renovate may only offer k3s releases inside the minor whose schemas are vendored
    # (#2367). A cap left behind after an upgrade blocks every later k3s PR, and one raised
    # ahead of the schemas brings back a PR that cannot pass CI until they are refreshed.
    assert _renovate_k3s_cap() == _cap_for(K8S_SCHEMA_VERSION), (
        f"renovate.json caps k3s-io/k3s at {_renovate_k3s_cap()!r}; with K8S_SCHEMA_VERSION "
        f"{K8S_SCHEMA_VERSION} it must be {_cap_for(K8S_SCHEMA_VERSION)!r}."
    )


def test_a_cap_one_minor_behind_the_schema_is_rejected():
    assert _cap_for("1.36") == "<1.37"
    assert _cap_for("1.37") != "<1.37"


# ── CRD schema validation ───────────────────────────────────────────────────────────────────
# Kubernetes' own OpenAPI files have no schema for a CRD — a CRD's schema lives in the cluster —
# so every Traefik object in this tree (IngressRoute, Middleware,
# TLSOption) would be counted as skipped and checked by nothing. They validate against the
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
    # apiVersion "v1" carries no group, so there is nothing to look up — the core check owns
    # core objects and must not be shadowed by a vendored CRD file.
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
