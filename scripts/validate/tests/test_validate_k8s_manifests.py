#!/usr/bin/env python3
"""Tests for the k8s manifest validator's duplicate-key check.

A repeated mapping key is valid YAML — the later value silently wins — so kubectl
applies the document, every check goes green, and only the losing setting is gone.
It bit homepage's pod spec, which acquired both `automountServiceAccountToken: true`
(needed by its kubernetes widget) and a `false` from an estate-wide sweep when the
two edits met in a rebase.

Run: uv run pytest scripts/validate/tests/test_validate_k8s_manifests.py
"""

import contextlib
import io
import re

import pytest

import validate_k8s_manifests as vkm
from typing import Any


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
def real_tree():
    """Run the validator over the real tree ONCE per worker: (exit code, stdout, stderr).

    A full run renders every k8s role and costs ~7s (2026-09-01; ~3.8s when this was written).
    Six guards below assert on different parts of the same run's output, and until 2026-09-01
    two fixtures each paid for their own run — one capturing stderr, one stdout — so every
    worker rendered the tree twice. The run is a pure function of the repo tree, so one
    capture of both streams serves all of them.

    # DECIDED: redirect_stderr/redirect_stdout, not capsys. capsys is function-scoped and
    # pytest refuses to inject it into a module-scoped fixture; main() writes with
    # print(file=sys.stderr), which redirect_stderr captures because it rebinds sys.stderr at
    # call time.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = vkm.main()
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture(scope="module")
def real_tree_run(real_tree):
    """(exit code, stderr) of the shared run — the shape the stderr guards read."""
    rc, _out, err = real_tree
    return rc, err


def test_real_tree_has_no_unresolved_claim_name(real_tree_run):
    # The actual regression guard: every claimName across the real k8s roles must resolve
    # against a rendered PVC (or a volume-claim-backed one) — a brand-new service naming a PVC
    # that was never wired up must show here, since nothing else in the tree checks this.
    rc, err = real_tree_run
    assert rc == 0
    assert "matches no rendered PersistentVolumeClaim" not in err


def test_volume_claim_pvc_names_resolves_a_real_claim_backed_role():
    # tdarr's config PVC is created by volume-claim's own pvc.yaml.j2 (never rendered under
    # volume-claim's own role — it's in SKIP_ROLES), using vars tdarr's include_role task passes.
    # tdarr's deployment.yaml.j2 references the SAME value directly as a claimName, so without
    # this resolving, tdarr's config claim would show as unresolved on every real run.
    base = {
        **vkm.BASE_CONTEXT,
        **vkm.load_yaml(vkm.ALL_VARS),
        "playbook_dir": str(vkm.ANSIBLE),
    }
    base = vkm.resolve_vars(base, base)
    ctx = {**base, **vkm.role_defaults("tdarr", base)}
    names = vkm.volume_claim_pvc_names("tdarr", ctx)
    assert ctx["tdarr_k8s_configs_claim"] in names


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
    assert vkm.schema_error(crd) is None


def test_a_crd_with_no_vendored_schema_still_reports_no_schema():
    # The skip path is not gone, only narrowed: a CRD group nothing has vendored is still
    # counted and named rather than silently passed.
    crd = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": "example"},
        "spec": {},
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


# --- resolve_vars: values nested inside lists and dicts ---
#
# Ansible expands `{{ ... }}` wherever a string sits inside a variable's value, not only when
# the whole value IS a string. Scanning top-level strings alone left the braces in place one
# level down, so a list-valued variable reached a template unexpanded and rendered literal
# `{{ ... }}` into YAML — the same defect resolve_vars exists to prevent, one level deeper.
#
# `traefik_k8s_watched_namespaces` is the live case: a YAML list of namespace references,
# looped over by both rbac.yaml.j2 and static-config.yaml.j2.


def test_resolve_vars_expands_a_string_inside_a_list():
    resolved = vkm.resolve_vars(
        {"watched": ["{{ ns_a }}", "{{ ns_b }}"]},
        {"ns_a": "homelab", "ns_b": "longhorn"},
    )
    assert resolved["watched"] == ["homelab", "longhorn"]


def test_resolve_vars_expands_a_string_inside_a_dict():
    resolved = vkm.resolve_vars({"m": {"src": "{{ root }}/x.py"}}, {"root": "/srv"})
    assert resolved["m"] == {"src": "/srv/x.py"}


def test_resolve_vars_expands_through_a_list_of_dicts():
    """The shape autofix_bridge_modules uses — the nesting is two levels, not one."""
    resolved = vkm.resolve_vars(
        {"mods": [{"name": "a.py", "src": "{{ root }}/a.py"}]}, {"root": "/srv"}
    )
    assert resolved["mods"] == [{"name": "a.py", "src": "/srv/a.py"}]


def test_resolve_vars_leaves_a_brace_free_value_alone():
    """The accepting half: a brace-free value is left alone.

    Expansion must not rewrite values that held no template, and must not coerce non-strings. A
    recursive walk that stringified as it went would pass the three tests above and quietly turn
    every int and bool in the inventory into text.
    """
    values = {"ports": [80, 443], "on": True, "names": ["homelab"], "nested": {"n": 1}}
    assert vkm.resolve_vars(dict(values), {}) == values


# ── role defaults must not shadow the inventory ───────────────────────────────────────────────


def test_a_role_default_that_shadows_an_inventory_key_is_flagged():
    """The rejecting half.

    `{**base, **role_defaults(...)}` ranks role defaults ABOVE the group_vars and host_vars in
    `base`, which is the reverse of Ansible's own precedence — so a shared key renders a value no
    deploy would produce, while staying valid YAML and passing the schema check.
    """
    assert vkm.colliding_default_keys(
        {"crowdsec_k8s_image": "role-value", "own_key": 1},
        {"crowdsec_k8s_image": "inventory-value", "other": 2},
    ) == {"crowdsec_k8s_image"}


def test_a_role_default_with_its_own_key_space_is_clean():
    """The accepting half — a rule that flagged everything would pass the test above too."""
    assert (
        vkm.colliding_default_keys({"sonarr_port": 8989}, {"domain": "example.com"})
        == set()
    )


def test_no_real_role_shadows_an_inventory_key(real_tree_run):
    """The regression guard, over the real tree: 54 roles, zero collisions when this landed."""
    _rc, err = real_tree_run
    assert "redefines inventory key" not in err


# --- an https IngressRoute must declare spec.tls ---------------------------------------------


def _route(entrypoints, tls=None):
    doc = {
        "kind": "IngressRoute",
        "metadata": {"name": "thing"},
        "spec": {"entryPoints": entrypoints, "routes": []},
    }
    if tls is not None:
        doc["spec"]["tls"] = tls
    return doc


def test_an_https_route_without_tls_is_flagged():
    """Without `tls:` Traefik never treats the route as a TLS router, so it never matches."""
    assert "spec.tls" in vkm.https_route_without_tls(_route(["https"]))


def test_an_https_route_with_tls_is_clean():
    assert vkm.https_route_without_tls(_route(["https"], tls={})) is None


def test_an_https_route_with_tls_but_no_cert_resolver_is_clean():
    """An empty resolver means Traefik's own self-signed cert — legitimate, and not this rule."""
    assert (
        vkm.https_route_without_tls(
            _route(["https"], tls={"options": {"name": "modern"}})
        )
        is None
    )


def test_a_non_https_route_without_tls_is_clean():
    assert vkm.https_route_without_tls(_route(["web"])) is None


def test_a_non_ingressroute_is_clean():
    assert vkm.https_route_without_tls({"kind": "Service", "spec": {}}) is None


def test_every_real_https_route_declares_tls(real_tree_run):
    _rc, err = real_tree_run
    assert "declares no spec.tls" not in err


# --- a NetworkPolicy port is the container's, not the Service's -------------------------------

_WORKLOAD = {
    "kind": "Deployment",
    "spec": {
        "template": {
            "metadata": {"labels": {"app": "traefik"}},
            "spec": {"containers": [{"ports": [{"containerPort": 8000}]}]},
        }
    },
}
_TRANSLATING_SERVICE = {
    "kind": "Service",
    "spec": {"ports": [{"port": 80, "targetPort": 8000}]},
}


def _policy(port):
    return {
        "kind": "NetworkPolicy",
        "metadata": {"name": "fence"},
        "spec": {
            "podSelector": {"matchLabels": {"app": "traefik"}},
            "ingress": [{"ports": [{"port": port}]}],
        },
    }


def _mismatches(policy, docs):
    return vkm.netpol_port_mismatches(
        policy,
        vkm.workload_container_ports(docs),
        vkm.service_port_translations(docs),
    )


def test_a_policy_using_the_service_port_is_flagged():
    docs = [_WORKLOAD, _TRANSLATING_SERVICE]
    assert _mismatches(_policy(80), docs)


def test_a_policy_using_the_container_port_is_clean():
    docs = [_WORKLOAD, _TRANSLATING_SERVICE]
    assert _mismatches(_policy(8000), docs) == []


def test_an_undeclared_listener_port_is_not_flagged():
    """traefik's metrics endpoint answers on 8080 while declaring no containerPort.

    A containerPort declaration is informational, so "not declared" alone is not evidence of a
    mistake. Only a number that a Service publishes to a DIFFERENT target is.
    """
    docs = [_WORKLOAD, _TRANSLATING_SERVICE]
    assert _mismatches(_policy(8080), docs) == []


def test_a_service_mapping_one_to_one_creates_no_confusion():
    docs = [
        _WORKLOAD,
        {"kind": "Service", "spec": {"ports": [{"port": 9000, "targetPort": 9000}]}},
    ]
    assert _mismatches(_policy(9000), docs) == []


def test_a_policy_selecting_no_workload_is_clean():
    """No matching workload is no evidence, and must not read as a finding."""
    policy = _policy(80)
    policy["spec"]["podSelector"] = {"matchLabels": {"app": "nothing"}}
    assert _mismatches(policy, [_WORKLOAD, _TRANSLATING_SERVICE]) == []


def test_an_empty_pod_selector_is_clean():
    """A bare `podSelector: {}` selects every pod in the namespace — no single port to check."""
    policy = _policy(80)
    policy["spec"]["podSelector"] = {}
    assert _mismatches(policy, [_WORKLOAD, _TRANSLATING_SERVICE]) == []


def test_the_real_tree_has_no_netpol_port_mismatch(real_tree_run):
    _rc, err = real_tree_run
    assert "is a Service's published port" not in err


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
    path = vkm.crd_schema_path(_ingressroute({"routes": [_ROUTE]}))
    assert path is not None
    assert path.parent.name == "traefik.io"
    assert path.name == "ingressroute_v1alpha1.json"


def test_a_core_object_has_no_crd_schema_path():
    # apiVersion "v1" carries no group, so there is nothing to look up — kubernetes_validate
    # owns core objects and must not be shadowed by a vendored file.
    assert vkm.crd_schema_path({"apiVersion": "v1", "kind": "Service"}) is None


def test_a_valid_ingressroute_is_clean():
    assert vkm.crd_schema_error(_ingressroute({"routes": [_ROUTE]})) is None


def test_a_misspelled_spec_key_is_flagged():
    # `entrypoints` for `entryPoints`. The API server ignores an unknown field, so the object
    # applies clean and the route simply never binds to the entrypoint — the silent class.
    err = vkm.crd_schema_error(
        _ingressroute({"entrypoints": ["https"], "routes": [_ROUTE]})
    )
    assert isinstance(err, str) and "entrypoints" in err


def test_a_misspelled_route_key_is_flagged():
    # `middleware` for `middlewares`, one level deeper than the spec.
    route = dict(_ROUTE, middleware=[{"name": "m"}])
    err = vkm.crd_schema_error(_ingressroute({"routes": [route]}))
    assert isinstance(err, str) and "middleware" in err


def test_a_route_without_match_is_flagged():
    err = vkm.crd_schema_error(_ingressroute({"routes": [{"kind": "Rule"}]}))
    assert isinstance(err, str) and "match" in err


def test_an_unknown_crd_kind_reports_no_schema():
    unknown = {"apiVersion": "example.com/v1", "kind": "Widget", "spec": {}}
    assert vkm.crd_schema_error(unknown) is vkm.NO_SCHEMA


def test_the_schema_does_not_catch_a_missing_tls_block():
    """The limit of structural validation, asserted so nobody claims coverage it lacks.

    `tls` is optional in the IngressRoute CRD — plain-HTTP routes are legal — so an https route
    with no `spec.tls` is a valid document and passes here, while silently never matching.
    `https_route_without_tls` is what catches that, and this test fails if someone ever removes
    it believing the schema had taken over.
    """
    doc = _ingressroute({"entryPoints": ["https"], "routes": [_ROUTE]})
    assert vkm.crd_schema_error(doc) is None
    assert vkm.https_route_without_tls(doc) is not None


@pytest.fixture(scope="module")
def real_tree_stdout(real_tree):
    """(exit code, stdout) of the shared run — the coverage tail is printed there, not stderr."""
    rc, out, _err = real_tree
    return rc, out


_UNCOVERED = "matched neither the v"


def test_the_real_tree_leaves_no_object_unchecked(real_tree_stdout):
    """Coverage, enforced rather than observed.

    This is the staleness answer for the vendored schemas: a new CRD kind — or a rename that
    stops an existing schema matching — makes the validator report the object as uncovered, and
    this fails. Refresh or add one with scripts/validate/refresh_crd_schemas.py.
    """
    rc, out = real_tree_stdout
    assert rc == 0
    assert _UNCOVERED not in out, out[out.find(_UNCOVERED) - 80 :][:400]


def test_the_uncovered_guard_can_fire(monkeypatch, tmp_path):
    """The rejecting half of the test above: with no vendored schemas, the tail must appear.

    Without this, a guard asserting the ABSENCE of a phrase would pass identically if the
    phrase were never printable — the failure mode test_every_validator_has_a_red_proof exists
    for, applied to a coverage claim instead of a validation one.
    """
    monkeypatch.setattr(vkm, "CRD_SCHEMA_DIR", tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        vkm.main()
    out = buf.getvalue()
    assert _UNCOVERED in out
    assert "traefik.io/v1alpha1/IngressRoute" in out
