#!/usr/bin/env python3
"""Schema validation for a rendered k8s object: the core OpenAPI check and the vendored CRDs.

Both halves read schemas vendored under ``scripts/validate/schemas/``, which
``refresh_vendored_schemas.py`` writes. The core half reads Kubernetes' own per-group OpenAPI v3
files, taken from the ``kubernetes/kubernetes`` tag that ``k3s_version`` names, so a new minor
is checkable the day k3s ships it rather than when a third-party package republishes it (#2367).
``CRD_SCHEMA_DIR`` is built from ``repo_paths.SCRIPTS`` rather than from this file's own
``__file__`` for the same reason the refresher writes there: the schemas stay beside the
validator that reads them.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import functools
import json
import re
from pathlib import Path

import jsonschema

from lib.repo_paths import SCRIPTS

__all__ = [
    "CORE_SCHEMA_DIR",
    "CRD_SCHEMA_DIR",
    "K8S_SCHEMA_VERSION",
    "NO_SCHEMA",
    "crd_schema_error",
    "crd_schema_path",
    "core_schema_file",
    "normalise_octal",
    "schema_error",
    "strict_schema",
]

# Schema version the rendered manifests are validated against. Must track the cluster: a
# manifest is judged by the API server it will actually be applied to, and validating a 1.37
# field against 1.36 schemas reports a perfectly good manifest as invalid (and vice versa —
# a removed field passes). test_vendored_core_schemas_match_the_cluster in
# scripts/validate/tests/test_k8s_schema.py ties this, and the exact tag the vendored files came
# from, to k3s_version in roles/setup/k3s/defaults/main.yml, so a cluster upgrade cannot leave
# it behind silently.
K8S_SCHEMA_VERSION = "1.37"

_OCTAL_LITERAL = re.compile(r"^0o[0-7]+$")

# Returned by schema_error for a kind the upstream OpenAPI spec does not describe — a CRD.
NO_SCHEMA = object()


def normalise_octal(node):
    """Convert YAML-1.2 octal literals (``0o444``) to the ints kubectl reads them as.

    PyYAML implements YAML **1.1**, where ``0o444`` is not a number and parses as the STRING
    "0o444"; the parser behind ``kubectl`` reads it as 292. So a manifest that is correct live
    arrives here with a string in an integer field, and the schema check would report four
    perfectly good ``defaultMode: 0o444`` volumes as type errors.

    This is not a guess about which parser wins. The live objects were read while writing this:
    ``scrutiny-web``, ``scrutiny-influxdb`` and ``uptime-kuma`` all carry
    ``secret.defaultMode: 292`` — 0444 — from exactly those templates.

    (The comment above mosquitto's ``defaultMode: 288`` claims the opposite, that kubectl reads
    ``0o440`` as a string. The live values disagree with it. Decimal is still the unambiguous
    spelling and mosquitto is fine as it stands, so nothing is changed there — but do not take
    that comment as the reason to avoid octal literals.)
    """
    if isinstance(node, dict):
        return {k: normalise_octal(v) for k, v in node.items()}
    if isinstance(node, list):
        return [normalise_octal(v) for v in node]
    if isinstance(node, str) and _OCTAL_LITERAL.match(node):
        return int(node, 8)
    return node


CRD_SCHEMA_DIR = SCRIPTS / "validate" / "schemas"
CORE_SCHEMA_DIR = CRD_SCHEMA_DIR / "kubernetes.io" / f"v{K8S_SCHEMA_VERSION}"


def core_schema_file(api_version: str) -> Path:
    """The vendored OpenAPI v3 file for a core apiVersion, named as upstream names it.

    ``v1`` is ``api__v1_openapi.json`` and ``apps/v1`` is ``apis__apps__v1_openapi.json``, the
    layout of ``api/openapi-spec/v3/`` in the kubernetes/kubernetes repo. The path is returned
    whether or not the file exists; a CRD's group has none.
    """
    if "/" not in api_version:
        return CORE_SCHEMA_DIR / f"api__{api_version}_openapi.json"
    group, _, version = api_version.partition("/")
    return CORE_SCHEMA_DIR / f"apis__{group}__{version}_openapi.json"


def strict_schema(node, *, root: bool = True, nullable: bool = True):
    """Kubernetes' OpenAPI schema made strict, as kubernetes-validate's ``-strict`` set was.

    Two changes, and only these two, because they are the whole difference between the upstream
    spec and the schemas this check used before #2367:

    - ``additionalProperties: false`` on every object that lists ``properties`` and sets no
      ``additionalProperties`` of its own. This is the half that catches a misspelled key. An
      object with no ``properties`` (``RawExtension``, a label map) stays open.
    - every ``type`` also admits ``null``, because the API server treats an explicit null as an
      omitted field. Two places keep their type as written: the root, and a property its object
      lists in ``required``, where null would stand in for the missing field. ``IntOrString``
      and ``Quantity`` are already ``oneOf`` string/integer (string/number) upstream, and get the
      same treatment per branch.

    Keys inside ``properties`` are field names, not keywords, so they are never rewritten.
    """
    if isinstance(node, list):
        return [strict_schema(v, root=False) for v in node]
    if not isinstance(node, dict):
        return node
    required = set(node.get("required") or ())
    out = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            out[key] = {
                name: strict_schema(sub, root=False, nullable=name not in required)
                for name, sub in value.items()
            }
        elif key in ("items", "additionalProperties", "allOf", "oneOf", "anyOf", "not"):
            out[key] = strict_schema(value, root=False)
        elif key == "type" and isinstance(value, str) and not root and nullable:
            out[key] = [value, "null"]
        else:
            out[key] = value
    if "properties" in out and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


@functools.cache
def _core_validators(
    path: Path,
) -> dict[tuple[str, str, str], jsonschema.Validator] | None:
    """One validator per group/version/kind in a vendored group file, or None if it is absent.

    Each validator's root is ``{"type": "object", "$ref": <the kind's component>}``, so the
    object itself may not be null even though every component, the kind's own included, is made
    nullable for its nested uses. ``$ref``s resolve against the file's own ``components``, which
    the refresher checks are self-contained.
    """
    if not path.is_file():
        return None
    components = json.loads(path.read_text())["components"]["schemas"]
    strict = {
        name: strict_schema(schema, root=False) for name, schema in components.items()
    }
    validators = {}
    for name, schema in components.items():
        for gvk in schema.get("x-kubernetes-group-version-kind", []):
            root = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "$ref": f"#/components/schemas/{name}",
                "components": {"schemas": strict},
            }
            validators[(gvk["group"], gvk["version"], gvk["kind"])] = (
                jsonschema.Draft202012Validator(root)
            )
    return validators


def crd_schema_path(doc: dict) -> Path | None:
    """Where a vendored JSON Schema for this object's apiVersion/kind would live, or None.

    Mirrors datreeio/CRDs-catalog's layout — ``<group>/<lowercase kind>_<version>.json`` — so
    refresh_vendored_schemas.py can pull straight from it with no per-kind mapping. A core object
    (apiVersion ``v1``, no group) has no slash and returns None; `schema_error` owns those.
    """
    api_version = doc.get("apiVersion")
    kind = doc.get("kind")
    if not isinstance(api_version, str) or not isinstance(kind, str):
        return None
    if "/" not in api_version:
        return None
    group, _, version = api_version.partition("/")
    return CRD_SCHEMA_DIR / group / f"{kind.lower()}_{version}.json"


def crd_schema_error(doc: dict) -> str | None | object:
    """Validate one CRD object against its vendored JSON Schema, or NO_SCHEMA if none exists.

    WHAT THIS CATCHES, precisely — it is narrower than it looks and the difference matters.
    The catalog's schemas set ``additionalProperties: false`` on the spec and on each route, so
    a misspelled key is rejected: ``entrypoints`` for ``entryPoints``, ``middleware`` for
    ``middlewares``. They also require ``spec.routes`` and each route's ``match``, and they
    type-check values. That is the same silent class the core check's ``strict=True`` covers —
    the API server ignores an unknown field, so the object applies clean and the setting simply
    never takes effect.

    WHAT IT DOES NOT CATCH: anything semantic. An https IngressRoute with no ``spec.tls`` is a
    valid document and passes here — ``tls`` is optional in the CRD, because plain-HTTP routes
    are legal. That bug class is `https_route_without_tls` below, and it stays the thing that
    catches it. Verified against the vendored schema rather than assumed.
    """
    path = crd_schema_path(doc)
    if path is None or not path.is_file():
        return NO_SCHEMA
    try:
        schema = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return f"vendored schema {path.name} is unreadable: {exc}"
    try:
        jsonschema.validate(normalise_octal(doc), schema)
    except jsonschema.ValidationError as exc:
        where = ".".join(str(p) for p in exc.absolute_path) or "<root>"
        return f"{where}: {exc.message}"
    except jsonschema.SchemaError as exc:
        return f"vendored schema {path.name} is invalid: {exc.message}"
    return None


def schema_error(doc: dict) -> str | None | object:
    """Validate one rendered object against the Kubernetes schema for K8S_SCHEMA_VERSION.

    Returns None when the object validates, NO_SCHEMA when nothing can check its
    apiVersion/kind, and an error string otherwise.

    The schema is strict (`strict_schema`): it rejects fields the API does not define, which is
    the half that catches typos. A misspelled ``readinessProb`` is silently ignored by the API
    server, so the Deployment applies clean and the probe simply never runs.

    A CRD has no schema in the upstream OpenAPI spec — it lives in the cluster — so its group
    has no vendored core file, and it falls through to `crd_schema_error` and the vendored
    catalog schemas. Before that existed, 60 of this tree's objects (46 IngressRoute,
    11 Middleware, 3 TLSOption) were counted as skipped and checked by nothing.

    This is the check ``--dry-run`` performs against the live API server, done offline and
    without a cluster. Formats (``date-time`` and the like) are not asserted, matching
    kubernetes-validate, which this replaced.
    """
    api_version = doc.get("apiVersion")
    kind = doc.get("kind")
    if not isinstance(api_version, str) or not isinstance(kind, str):
        return crd_schema_error(doc)
    group, _, version = api_version.rpartition("/")
    validators = _core_validators(core_schema_file(api_version))
    validator = (validators or {}).get((group, version, kind))
    if validator is None:
        return crd_schema_error(doc)
    error = jsonschema.exceptions.best_match(
        validator.iter_errors(normalise_octal(doc))
    )
    if error is None:
        return None
    where = ".".join(str(p) for p in error.absolute_path)
    return f"{where}: {error.message}" if where else error.message
